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
<title>Voice agent, under the hood</title>
<style>
:root{color-scheme:light}
body{margin:0;padding:40px 28px;background:#fbfbfa;color:#1a1a18;
 font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:880px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px}
p.sub{margin:0 0 8px;color:#78756e}
p.stack{margin:0 0 26px;color:#a8a49b;font-size:12px}
.ctl{display:flex;gap:10px;align-items:center}
button{font:inherit;padding:11px 22px;border:1px solid #1a1a18;border-radius:8px;
 background:#1a1a18;color:#fff;cursor:pointer}
button.ghost{background:#fff;color:#1a1a18}
button:disabled{opacity:.35;cursor:default}
.total{margin-left:auto;font-size:26px;letter-spacing:-.5px;color:#1a1a18}
.total span{font-size:13px;color:#a8a49b;letter-spacing:0}
.hint{margin:14px 0 26px;color:#78756e;font-size:13px;min-height:20px}
.hint b{color:#1a1a18}
.err{color:#b91c1c}

.axis{position:relative;height:15px;margin:0 0 2px 176px;border-bottom:1px solid #eceae5}
.axis .dl{position:absolute;top:0;bottom:0;width:2px;background:#dc2626}
.axis .dlv{position:absolute;top:-2px;font-size:10px;color:#dc2626;
 transform:translateX(-100%);padding-right:5px;white-space:nowrap}

.stage{display:grid;grid-template-columns:12px 150px 1fr 70px;gap:14px;
 align-items:center;padding:11px 0;border-bottom:1px solid #f2f0ec}
.dot{width:9px;height:9px;border-radius:50%;background:#dcd9d2;transition:background .15s}
.stage.run .dot{background:#1a1a18;animation:pulse 1s infinite}
.stage.ok .dot{background:#3f8f5f}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.2}}
.name b{display:block;font-size:12.5px;color:#a8a49b}
.name i{font-style:normal;color:#c4c0b7;font-size:10.5px}
.stage.run .name b,.stage.ok .name b{color:#1a1a18}
.track{position:relative;height:22px;background:#f4f3f0;border-radius:3px;overflow:hidden}
.bar{position:absolute;top:0;height:22px;border-radius:3px;border:1px solid #2b2a27;
 box-sizing:border-box;width:0}
.stage.run .bar{border-style:dashed}
.ms{text-align:right;font-size:12.5px;color:#c4c0b7;font-variant-numeric:tabular-nums}
.stage.run .ms{color:#1a1a18}
.stage.ok .ms{color:#3f8f5f}
.say{margin:3px 0 0 176px;color:#6b6862;font-size:12px;min-height:16px;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
footer{margin-top:30px;color:#a8a49b;font-size:12px;line-height:1.75}
code{background:#f1efea;padding:1px 5px;border-radius:3px}
</style></head><body><main>
<h1>Voice agent, under the hood</h1>
<p class="sub">Press start, then talk to your machine. Watch where the time goes.</p>
<p class="stack" id="stack"></p>

<div class="ctl">
  <button id="go">Start listening</button>
  <button id="halt" class="ghost" disabled>Stop</button>
  <div class="total"><span id="tlab">since you stopped speaking</span>
    <div id="total">0 ms</div></div>
</div>
<p class="hint" id="hint">Uses this machine's microphone and speakers.
<b>Wear headphones</b> — otherwise the agent hears itself.</p>

<div class="axis" id="axis"><div class="dl" id="dl"></div>
  <div class="dlv" id="dlv">800 ms</div></div>

<div id="stages">
  <div class="stage" id="s-vad"><div class="dot"></div>
    <div class="name"><b>Voice activity</b><i>are you still talking?</i></div>
    <div class="track"><div class="bar" style="background:#fff"></div></div>
    <div class="ms"></div></div>
  <div class="say" id="say-vad"></div>

  <div class="stage" id="s-stt"><div class="dot"></div>
    <div class="name"><b>Speech to text</b><i>audio becomes words</i></div>
    <div class="track"><div class="bar" style="background:#d9d7d1"></div></div>
    <div class="ms"></div></div>
  <div class="say" id="say-stt"></div>

  <div class="stage" id="s-llm"><div class="dot"></div>
    <div class="name"><b>Language model</b><i>words become a reply</i></div>
    <div class="track"><div class="bar" style="background:#8d8a83"></div></div>
    <div class="ms"></div></div>
  <div class="say" id="say-llm"></div>

  <div class="stage" id="s-tts"><div class="dot"></div>
    <div class="name"><b>Text to speech</b><i>the reply becomes audio</i></div>
    <div class="track"><div class="bar" style="background:#3f3d39"></div></div>
    <div class="ms"></div></div>
  <div class="say" id="say-tts"></div>
</div>

<footer>
Each bar starts where that stage started and grows while it runs, so bars
overlapping vertically were running at the same time. Dashed means still going.
The red line is 800&nbsp;ms, roughly what human turn-taking costs.<br>
This is the live view. The authoritative per-stage numbers come from the
OpenTelemetry spans <code>budget.py</code> reads.<br>
Swap a component and run again: <code>VOICE_TTS_ENGINE=piper make live</code>
</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const el = s => $("s-" + s);
const STAGES = ["vad","stt","llm","tts"];
let scale = 2500;          // ms across the full width, grows as a turn runs
let open = {};             // stage -> start ms, while running
let turn0 = null;          // wall clock when this turn began

function setScale(ms){
  const want = Math.max(2500, Math.ceil(ms / 500) * 500 * 1.15);
  if(want !== scale){
    scale = want;
    STAGES.forEach(s => { const b = el(s).querySelector(".bar");
      if(b.dataset.start) place(s, +b.dataset.start, +(b.dataset.end || 0)); });
  }
  const pct = Math.min(100, 800 / scale * 100);
  $("dl").style.left = pct + "%"; $("dlv").style.left = pct + "%";
}
function place(s, startMs, endMs){
  const b = el(s).querySelector(".bar");
  b.dataset.start = startMs; if(endMs) b.dataset.end = endMs;
  b.style.left  = (startMs / scale * 100) + "%";
  b.style.width = (Math.max(endMs - startMs, 6) / scale * 100) + "%";
}
function reset(){
  open = {}; turn0 = performance.now();
  STAGES.forEach(s => { const e = el(s); e.className = "stage";
    const b = e.querySelector(".bar");
    b.style.width = "0"; b.style.left = "0";
    delete b.dataset.start; delete b.dataset.end;
    e.querySelector(".ms").textContent = ""; $("say-"+s).textContent = ""; });
  scale = 2500; setScale(2500); $("total").textContent = "0 ms";
}
// Grow the running bars locally between events, so the page moves continuously
// instead of jumping only when the pipeline happens to emit something.
function tick(){
  if(turn0 !== null){
    const now = performance.now() - turn0;
    setScale(now);
    $("total").textContent = Math.round(now) + " ms";
    for(const s in open){
      place(s, open[s], now);
      el(s).querySelector(".ms").textContent = Math.round(now - open[s]) + " ms";
    }
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

function hint(t, bad){ $("hint").innerHTML = t; $("hint").className = "hint" + (bad?" err":""); }
$("go").onclick = async () => {
  $("go").disabled = true; reset(); turn0 = null;
  hint("Loading models… the first run downloads and warms them, which can take "
     + "a minute. Nothing is listening yet.");
  const r = await (await fetch("/start", {method:"POST"})).json();
  if(!r.ok){ hint(r.error, true); $("go").disabled = false; }
};
$("halt").onclick = async () => {
  $("halt").disabled = true; hint("stopping…");
  await fetch("/stop", {method:"POST"});
};

const es = new EventSource("/events");
es.onmessage = m => {
  const e = JSON.parse(m.data);
  if(e.kind === "ready"){ $("stack").textContent = e.text; $("halt").disabled = false;
    hint("<b>Listening.</b> Say something."); return; }
  if(e.kind === "error"){ hint(e.text, true); return; }
  if(e.kind === "stopped"){ $("go").disabled = false; $("halt").disabled = true;
    turn0 = null; open = {}; hint("Stopped."); return; }
  if(e.kind === "reset"){ reset(); return; }
  if(e.kind === "turn_complete"){ turn0 = null; open = {};
    $("tlab").textContent = "this turn took";
    hint("Your turn. Say something else."); return; }

  const box = el(e.stage); if(!box) return;
  if(e.kind === "begin"){
    open[e.stage] = e.ms; box.className = "stage run";
    place(e.stage, e.ms, e.ms + 6);
    if(e.text) $("say-"+e.stage).textContent = e.text;
  }
  if(e.kind === "first"){
    // first token / first audio byte: the number that actually matters here
    box.querySelector(".ms").textContent = Math.round(e.ms - (open[e.stage]||0)) + " ms";
    if(e.text) $("say-"+e.stage).textContent = e.text;
  }
  if(e.kind === "token"){ $("say-"+e.stage).textContent += e.text; }
  if(e.kind === "end"){
    delete open[e.stage];
    box.className = "stage ok";
    place(e.stage, e.start_ms, e.ms);
    box.querySelector(".ms").textContent = Math.round(e.ms - e.start_ms) + " ms";
    if(e.text) $("say-"+e.stage).textContent = e.text;
  }
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
