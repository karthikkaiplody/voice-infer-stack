"""Talk to the agent and watch the stages light up.

    make live          # then open http://localhost:8080

Click Start, speak into your machine's microphone, and the reply plays through
its speakers. The browser shows the request moving through the pipeline as it
happens: voice activity detected, your turn ends, audio becomes text, text
becomes a reply, the reply becomes audio.

This is the observability piece, not a benchmark. There is no comparison here
and no second build. One pipeline, the same one `bench.py` measures, with the
components `factory.py` gives it. Swap a component and this page shows the new
shape.

The microphone is the one attached to this machine, not the browser's. That
keeps the whole thing to a local pipeline plus a page of events, with no WebRTC
and no browser audio permissions.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from aiohttp import web
from loguru import logger

import factory
from config import CONFIG

# One queue PER connected browser. A single shared asyncio.Queue is consumed
# rather than broadcast, so with two tabs open each event would reach only one
# of them and both views would be wrong in different ways.
CLIENTS: list[asyncio.Queue] = []
LAST_READY: dict | None = None
RUNNING = False
RUNNER = None      # the WorkerRunner, so Stop can cancel it
TASK: asyncio.Task | None = None   # the task running it, so shutdown can join it
SHUTDOWN: asyncio.Event | None = None  # set on ctrl+c, releases the SSE loops


def emit(kind: str, stage: str = "", text: str = "", ms: float | None = None):
    """Fan one event out to every connected browser."""
    global LAST_READY
    ev = {"kind": kind, "stage": stage, "text": text, "ms": ms, "t": time.time()}
    if kind == "ready":
        # Replayed to anyone who connects later, so a tab opened after the
        # pipeline started still learns which stack is running.
        LAST_READY = ev
    for q in list(CLIENTS):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


class StageObserver:
    """Turn pipeline frames into the events the page draws.

    Deliberately a thin translation layer: it reports what Pipecat already
    announces rather than timing anything itself. The authoritative numbers come
    from the OpenTelemetry spans that `budget.py` reads; this is the live view.
    """

    def __init__(self, **kwargs):
        self._t0 = None
        self._seen = set()
        self._open: dict[str, float] = {}   # stage -> ms offset it began at

    def _ms(self, now):
        return (now - self._t0) * 1000 if self._t0 else 0.0

    def _begin(self, stage, text, now):
        """A stage started. The page grows its bar from here until it ends."""
        self._open[stage] = self._ms(now)
        emit("begin", stage, text, self._open[stage])

    def _end(self, stage, text, now):
        """A stage finished. Freeze the bar at its real width."""
        start = self._open.pop(stage, self._ms(now))
        EV = {"kind": "end", "stage": stage, "text": text,
              "ms": self._ms(now), "start_ms": start, "t": time.time()}
        for q in list(CLIENTS):
            try:
                q.put_nowait(EV)
            except asyncio.QueueFull:
                pass

    async def on_push_frame(self, data):
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame,
            BotStoppedSpeakingFrame,
            LLMFullResponseEndFrame,
            LLMTextFrame,
            TranscriptionFrame,
            TTSAudioRawFrame,
            UserStartedSpeakingFrame,
            UserStoppedSpeakingFrame,
        )

        f = data.frame
        now = time.monotonic()

        if isinstance(f, UserStartedSpeakingFrame):
            self._t0 = now
            self._seen.clear()
            self._open.clear()
            emit("reset")
            self._begin("vad", "hearing you", now)
        elif isinstance(f, UserStoppedSpeakingFrame):
            self._end("vad", "you stopped; waiting to be sure", now)
            self._begin("stt", "transcribing", now)
        elif isinstance(f, TranscriptionFrame) and f.text:
            self._end("stt", f.text, now)
            self._begin("llm", "thinking", now)
        elif isinstance(f, LLMTextFrame) and f.text:
            if "llm_first" not in self._seen:
                self._seen.add("llm_first")
                # First token is the number that matters for the LLM stage.
                emit("first", "llm", f.text, self._ms(now))
            else:
                emit("token", "llm", f.text)
        elif isinstance(f, LLMFullResponseEndFrame):
            self._end("llm", "", now)
        elif isinstance(f, TTSAudioRawFrame):
            if "tts_first" not in self._seen:
                self._seen.add("tts_first")
                if "tts" not in self._open:
                    self._begin("tts", "speaking", now)
                emit("first", "tts", "first audio out", self._ms(now))
        elif isinstance(f, BotStartedSpeakingFrame):
            if "tts" not in self._open:
                self._begin("tts", "speaking", now)
        elif isinstance(f, BotStoppedSpeakingFrame):
            self._end("tts", "", now)
            emit("turn_complete", text=f"{self._ms(now):.0f}")

    async def setup(self, *a, **k):
        pass

    async def cleanup(self, *a, **k):
        pass


async def run_agent():
    """One live conversation, using this machine's microphone and speakers."""
    global RUNNING

    from pipecat.observers.base_observer import BaseObserver
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )
    from pipecat.turns.user_mute import AlwaysUserMuteStrategy
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.workers.runner import WorkerRunner

    class Obs(BaseObserver, StageObserver):
        def __init__(self, **kw):
            BaseObserver.__init__(self, **kw)
            StageObserver.__init__(self)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=CONFIG.input_sample_rate,
            audio_out_sample_rate=CONFIG.output_sample_rate,
        )
    )

    stt, llm, tts = factory.make_stt(), factory.make_llm(), factory.make_tts()
    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=factory.make_vad(),
            user_turn_strategies=UserTurnStrategies(
                stop=[SpeechTimeoutUserTurnStopStrategy(
                    user_speech_timeout=CONFIG.user_speech_timeout,
                    wait_for_transcript=CONFIG.wait_for_transcript,
                )]
            ),
            # THE ECHO FIX. Microphone and speakers are the same machine and
            # there is no acoustic echo cancellation, so without this the bot
            # hears its own voice, VAD treats it as you speaking, the reply is
            # interrupted mid-sentence, and the bot's own words get transcribed
            # back in as your next question. Observed in a real session: the
            # context filled with the bot answering itself about Die Hard.
            #
            # Muting input while the bot speaks also fixes a second symptom.
            # An interrupted assistant turn never commits to the context, so
            # every message accumulated as role=user and the conversation had
            # no assistant side at all.
            user_mute_strategies=[AlwaysUserMuteStrategy()],
        ),
    )

    pipeline = Pipeline([
        transport.input(), stt, user_agg, llm, tts,
        transport.output(), assistant_agg,
    ])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        observers=[Obs()],
    )
    global RUNNER
    runner = WorkerRunner(handle_sigint=False)
    RUNNER = runner
    await runner.add_workers(worker)

    emit("ready", text=factory.describe())
    try:
        await runner.run()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("pipeline failed")
        emit("error", text=f"{type(e).__name__}: {e}")
    finally:
        RUNNING = False
        RUNNER = None
        emit("stopped")


# ----------------------------------------------------------------- web -----

async def sse(request):
    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
    })
    await resp.prepare(request)
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    CLIENTS.append(q)
    stopping = asyncio.ensure_future(SHUTDOWN.wait())
    try:
        if LAST_READY:
            await resp.write(f"data: {json.dumps(LAST_READY)}\n\n".encode())
        while not SHUTDOWN.is_set():
            nxt = asyncio.ensure_future(q.get())
            done, _ = await asyncio.wait(
                {nxt, stopping}, timeout=15,
                return_when=asyncio.FIRST_COMPLETED)
            if stopping in done:
                nxt.cancel()
                break
            if nxt in done:
                await resp.write(f"data: {json.dumps(nxt.result())}\n\n".encode())
            else:
                nxt.cancel()
                await resp.write(b": keepalive\n\n")
    except ConnectionResetError:
        pass
    except asyncio.CancelledError:
        # Do NOT swallow this. Returning a response from a cancelled handler
        # makes aiohttp complete an already-completed waiter, which surfaced
        # as `InvalidStateError: invalid state` on every ctrl+c.
        raise
    finally:
        stopping.cancel()
        if q in CLIENTS:
            CLIENTS.remove(q)
    return resp


async def start(request):
    global RUNNING, TASK
    if RUNNING:
        return web.json_response({"ok": False, "error": "already running"})
    RUNNING = True
    # Held, not fire-and-forget. An untracked task keeps the event loop alive
    # after aiohttp has shut down, so ctrl+c appeared to do nothing.
    TASK = asyncio.create_task(run_agent())
    return web.json_response({"ok": True, "stack": factory.describe()})


async def stop(request):
    global RUNNING
    if not RUNNING or RUNNER is None:
        return web.json_response({"ok": False, "error": "not running"})
    try:
        await RUNNER.cancel()
    except Exception as e:
        logger.warning(f"cancel: {e}")
    RUNNING = False
    return web.json_response({"ok": True})


async def index(request):
    return web.Response(text=PAGE, content_type="text/html")


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Voice agent · live trace</title>
<style>
*{box-sizing:border-box}
:root{
  --bg:#0d0f12; --panel:#14171c; --line:#1e232a; --line2:#262c34;
  --ink:#e8eaed; --dim:#7d858f; --faint:#4a515b;
  --run:#5ac8fa; --ok:#3ddc97; --red:#ff5f56;
  --gut:190px;
}
body{margin:0;padding:32px 26px 60px;background:var(--bg);color:var(--ink);
 font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:1000px;margin:0 auto}
header{display:flex;align-items:baseline;gap:14px;margin-bottom:3px}
h1{font-size:16px;font-weight:600;margin:0;letter-spacing:-.2px}
.stack{color:var(--faint);font-size:11px;margin:0 0 22px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

.bar-top{display:flex;align-items:center;gap:9px;margin-bottom:18px}
button{font:inherit;font-size:12px;padding:7px 15px;border-radius:6px;cursor:pointer;
 border:1px solid var(--line2);background:var(--panel);color:var(--ink)}
button.primary{background:var(--ink);color:var(--bg);border-color:var(--ink)}
button:disabled{opacity:.3;cursor:default}
.spacer{flex:1}
.clock{text-align:right;line-height:1.1}
.clock .n{font-size:22px;letter-spacing:-.6px;font-variant-numeric:tabular-nums}
.clock .l{font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.7px}

.status{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim);
 margin-bottom:20px;min-height:17px}
.status .led{width:7px;height:7px;border-radius:50%;background:var(--faint);flex:none}
.status.live .led{background:var(--ok);box-shadow:0 0 0 3px rgba(61,220,151,.15)}
.status.busy .led{background:var(--run);animation:bl 1s infinite}
.status.bad{color:var(--red)} .status.bad .led{background:var(--red)}
@keyframes bl{0%,100%{opacity:1}50%{opacity:.25}}

.trace{background:var(--panel);border:1px solid var(--line);border-radius:9px;
 padding:0 0 6px;overflow:hidden}
.ticks{position:relative;height:26px;margin-left:var(--gut);
 border-bottom:1px solid var(--line2)}
.tick{position:absolute;top:0;bottom:0;border-left:1px solid var(--line)}
.tick span{position:absolute;top:6px;left:5px;font-size:10px;color:var(--faint);
 font-variant-numeric:tabular-nums;white-space:nowrap}
.unit{position:absolute;right:8px;top:6px;font-size:10px;color:var(--faint)}
.mark{position:absolute;top:0;bottom:-999px;border-left:1px dashed var(--red);
 opacity:.5;z-index:1}
.mark span{position:absolute;top:6px;left:5px;font-size:10px;color:var(--red)}
.head{position:absolute;top:0;bottom:-999px;width:1px;background:var(--run);
 box-shadow:0 0 7px 1px rgba(90,200,250,.5);z-index:3;display:none}
.head::after{content:"";position:absolute;top:-1px;left:-3px;width:7px;height:7px;
 border-radius:50%;background:var(--run)}

.row{position:relative;display:flex;align-items:stretch;min-height:34px;
 border-bottom:1px solid var(--line)}
.row:last-of-type{border-bottom:0}
.gut{width:var(--gut);flex:none;padding:8px 14px 8px 16px;border-right:1px solid var(--line2)}
.gut b{display:block;font-size:12px;font-weight:500;color:var(--faint);letter-spacing:-.1px}
.gut i{font-style:normal;font-size:10px;color:var(--faint);opacity:.65}
.row.run .gut b{color:var(--ink)} .row.ok .gut b{color:var(--dim)}
.lane{position:relative;flex:1}
.span{position:absolute;top:9px;height:16px;border-radius:3px;min-width:3px;
 background:var(--faint);transition:opacity .2s}
.row.run .span{background:var(--run)}
.row.ok  .span{background:var(--ok);opacity:.8}
.dur{position:absolute;top:11px;font-size:10.5px;color:var(--dim);white-space:nowrap;
 font-variant-numeric:tabular-nums}
.note{padding:0 16px 9px calc(var(--gut) + 14px);font-size:11px;color:var(--dim);
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:-4px}
.note:empty{display:none}

footer{margin-top:18px;color:var(--faint);font-size:11px;line-height:1.8}
code{color:var(--dim)}
</style></head><body><main>

<header><h1>Voice agent</h1><span style="color:var(--faint);font-size:11px">live trace</span></header>
<p class="stack" id="stack">&nbsp;</p>

<div class="bar-top">
  <button id="go" class="primary">Start listening</button>
  <button id="halt" disabled>Stop</button>
  <div class="spacer"></div>
  <div class="clock"><div class="l" id="tlab">elapsed</div><div class="n" id="total">—</div></div>
</div>

<div class="status" id="st"><span class="led"></span><span id="stx">Idle. Use headphones, or the agent hears itself.</span></div>

<div class="trace">
  <div class="ticks" id="ticks"><span class="unit">ms</span>
    <div class="mark" id="mark"><span>800</span></div>
    <div class="head" id="head"></div></div>

  <div class="row" id="r-vad"><div class="gut"><b>Voice activity</b><i>are you still talking?</i></div>
    <div class="lane"><div class="span"></div><div class="dur"></div></div></div>
  <div class="note" id="n-vad"></div>

  <div class="row" id="r-stt"><div class="gut"><b>Speech to text</b><i>audio becomes words</i></div>
    <div class="lane"><div class="span"></div><div class="dur"></div></div></div>
  <div class="note" id="n-stt"></div>

  <div class="row" id="r-llm"><div class="gut"><b>Language model</b><i>words become a reply</i></div>
    <div class="lane"><div class="span"></div><div class="dur"></div></div></div>
  <div class="note" id="n-llm"></div>

  <div class="row" id="r-tts"><div class="gut"><b>Text to speech</b><i>the reply becomes audio</i></div>
    <div class="lane"><div class="span"></div><div class="dur"></div></div></div>
  <div class="note" id="n-tts"></div>
</div>

<footer>
Spans start where the stage started and grow while it runs. Two spans covering
the same slice of the axis ran at the same time.<br>
Dashed red is 800&nbsp;ms, roughly what human turn-taking costs. Live view; the
authoritative numbers come from the OpenTelemetry spans <code>budget.py</code> reads.<br>
Swap a component: <code>VOICE_TTS_ENGINE=piper make live</code>
</footer>
</main>
<script>
const $=i=>document.getElementById(i), S=["vad","stt","llm","tts"];
let scale=2000, open={}, t0=null, frozen=null;

function ticks(){
  const box=$("ticks"), keep=[$("mark"),$("head"),box.querySelector(".unit")];
  [...box.children].forEach(c=>{ if(!keep.includes(c)) c.remove(); });
  const step = scale<=2000?250 : scale<=5000?500 : 1000;
  for(let ms=0; ms<=scale; ms+=step){
    const d=document.createElement("div"); d.className="tick";
    d.style.left=(ms/scale*100)+"%";
    d.innerHTML='<span>'+ms+'</span>'; box.appendChild(d);
  }
  $("mark").style.left=Math.min(100, 800/scale*100)+"%";
}
function rescale(ms){
  const want=Math.max(2000, Math.ceil(ms*1.12/500)*500);
  if(want!==scale){ scale=want; ticks(); S.forEach(redraw); }
}
function redraw(s){
  const r=$("r-"+s), sp=r.querySelector(".span"), du=r.querySelector(".dur");
  const a=+sp.dataset.a, b=+sp.dataset.b;
  if(isNaN(a)){ sp.style.width="0"; du.textContent=""; return; }
  const L=a/scale*100, W=Math.max((b-a)/scale*100, .35);
  sp.style.left=L+"%"; sp.style.width=W+"%";
  du.textContent=Math.round(b-a)+" ms";
  du.style.left=Math.min(L+W+1, 88)+"%";
}
function reset(){
  open={}; frozen=null; t0=performance.now(); scale=2000; ticks();
  S.forEach(s=>{ const r=$("r-"+s), sp=r.querySelector(".span");
    r.className="row"; delete sp.dataset.a; delete sp.dataset.b;
    redraw(s); $("n-"+s).textContent=""; });
  $("head").style.display="block";
}
function status(t,cls){ $("stx").textContent=t; $("st").className="status "+(cls||""); }

(function loop(){
  if(t0!==null){
    const now=performance.now()-t0;
    rescale(now);
    $("total").textContent=Math.round(now).toLocaleString()+" ms";
    $("head").style.left=Math.min(now/scale*100,100)+"%";
    for(const s in open){ const sp=$("r-"+s).querySelector(".span");
      sp.dataset.b=now; redraw(s); }
  }
  requestAnimationFrame(loop);
})();
ticks();

$("go").onclick=async()=>{ $("go").disabled=true; reset(); t0=null;
  $("total").textContent="—";
  status("Loading models. First run downloads and warms them; nothing is listening yet.","busy");
  const r=await(await fetch("/start",{method:"POST"})).json();
  if(!r.ok){ status(r.error,"bad"); $("go").disabled=false; } };
$("halt").onclick=async()=>{ $("halt").disabled=true; status("Stopping…","busy");
  await fetch("/stop",{method:"POST"}); };

new EventSource("/events").onmessage=m=>{
  const e=JSON.parse(m.data);
  if(e.kind==="ready"){ $("stack").textContent=e.text; $("halt").disabled=false;
    status("Listening. Say something.","live"); return; }
  if(e.kind==="error"){ status(e.text,"bad"); return; }
  if(e.kind==="stopped"){ $("go").disabled=false; $("halt").disabled=true;
    t0=null; open={}; $("head").style.display="none"; status("Stopped."); return; }
  if(e.kind==="reset"){ reset(); status("Hearing you.","busy"); return; }
  if(e.kind==="turn_complete"){ open={}; t0=null; $("head").style.display="none";
    $("tlab").textContent="this turn"; $("total").textContent=(+e.text).toLocaleString()+" ms";
    status("Listening. Say something.","live"); return; }

  const r=$("r-"+e.stage); if(!r) return;
  const sp=r.querySelector(".span");
  if(e.kind==="begin"){ open[e.stage]=e.ms; r.className="row run";
    sp.dataset.a=e.ms; sp.dataset.b=e.ms; redraw(e.stage);
    if(e.text) $("n-"+e.stage).textContent=e.text; }
  if(e.kind==="first"){ if(e.text) $("n-"+e.stage).textContent=e.text; }
  if(e.kind==="token"){ $("n-"+e.stage).textContent+=e.text; }
  if(e.kind==="end"){ delete open[e.stage]; r.className="row ok";
    sp.dataset.a=e.start_ms; sp.dataset.b=e.ms; redraw(e.stage);
    if(e.text) $("n-"+e.stage).textContent=e.text; }
};
</script></body></html>"""


async def release_clients(app):
    """Let every streaming response finish on its own terms.

    aiohttp closes connections between on_shutdown and on_cleanup. An SSE
    handler still parked on a queue at that moment gets cancelled mid-write,
    which is what produced the ctrl+c traceback.
    """
    if SHUTDOWN is not None:
        SHUTDOWN.set()
    await asyncio.sleep(0.1)


async def shutdown(app):
    """Ctrl+C: put the microphone down before leaving.

    The pipeline owns a PyAudio input stream. Exiting without cancelling the
    worker leaves the device open and the next run fails to acquire it, so a
    tidy exit here is what makes `make live` restartable.
    """
    global TASK
    if RUNNER is not None:
        logger.info("stopping the pipeline...")
        try:
            await RUNNER.cancel()
        except Exception:
            pass
    if TASK is not None and not TASK.done():
        TASK.cancel()
        try:
            # Bounded: a pipeline that will not let go must not hold the
            # terminal hostage. Exiting late is better than not exiting.
            await asyncio.wait_for(asyncio.shield(TASK), timeout=5)
        except BaseException:
            pass
        TASK = None


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    async def boot(app):
        global SHUTDOWN
        SHUTDOWN = asyncio.Event()

    app = web.Application()
    app.on_startup.append(boot)
    app.on_shutdown.append(release_clients)
    app.on_cleanup.append(shutdown)
    app.add_routes([web.get("/", index), web.get("/events", sse),
                    web.post("/start", start), web.post("/stop", stop)])
    logger.info(f"open http://localhost:{args.port}")
    logger.info(f"stack: {factory.describe()}")
    logger.info("ctrl+c to quit")
    from aiohttp.web_runner import GracefulExit

    try:
        web.run_app(app, port=args.port, print=None, handle_signals=True)
    except (KeyboardInterrupt, GracefulExit):
        # GracefulExit is how aiohttp reports SIGINT. Letting it escape prints
        # a traceback for what is a normal, requested exit.
        pass
    finally:
        # aiohttp already ran on_cleanup by here. This is only so the terminal
        # ends on a sentence rather than a traceback.
        logger.info("bye")


if __name__ == "__main__":
    main()
