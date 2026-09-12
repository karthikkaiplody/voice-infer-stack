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
            emit("reset")
            emit("active", "vad", "hearing you")
        elif isinstance(f, UserStoppedSpeakingFrame):
            emit("done", "vad", "turn ended",
                 (now - self._t0) * 1000 if self._t0 else None)
            emit("active", "stt", "transcribing")
        elif isinstance(f, TranscriptionFrame) and f.text:
            emit("done", "stt", f.text,
                 (now - self._t0) * 1000 if self._t0 else None)
            emit("active", "llm", "thinking")
        elif isinstance(f, LLMTextFrame) and f.text:
            if "llm_first" not in self._seen:
                self._seen.add("llm_first")
                emit("first_token", "llm", f.text,
                     (now - self._t0) * 1000 if self._t0 else None)
            else:
                emit("token", "llm", f.text)
        elif isinstance(f, LLMFullResponseEndFrame):
            emit("done", "llm", "", (now - self._t0) * 1000 if self._t0 else None)
        elif isinstance(f, TTSAudioRawFrame):
            if "tts_first" not in self._seen:
                self._seen.add("tts_first")
                emit("first_audio", "tts", "speaking",
                     (now - self._t0) * 1000 if self._t0 else None)
        elif isinstance(f, BotStartedSpeakingFrame):
            emit("active", "tts", "speaking")
        elif isinstance(f, BotStoppedSpeakingFrame):
            emit("done", "tts", "", (now - self._t0) * 1000 if self._t0 else None)
            emit("turn_complete")

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
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    emit("ready", text=factory.describe())
    try:
        await runner.run()
    finally:
        RUNNING = False
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
    try:
        if LAST_READY:
            await resp.write(f"data: {json.dumps(LAST_READY)}\n\n".encode())
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
                await resp.write(f"data: {json.dumps(ev)}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if q in CLIENTS:
            CLIENTS.remove(q)
    return resp


async def start(request):
    global RUNNING
    if RUNNING:
        return web.json_response({"ok": False, "error": "already running"})
    RUNNING = True
    asyncio.create_task(run_agent())
    return web.json_response({"ok": True, "stack": factory.describe()})


async def index(request):
    return web.Response(text=PAGE, content_type="text/html")


PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Voice agent, under the hood</title>
<style>
:root{color-scheme:light}
body{margin:0;padding:44px 28px;background:#fbfbfa;color:#1a1a18;
 font:15px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace}
main{max-width:760px;margin:0 auto}
h1{font-size:21px;margin:0 0 4px}
p.sub{margin:0 0 8px;color:#78756e}
p.stack{margin:0 0 30px;color:#a8a49b;font-size:12px}
button{font:inherit;padding:11px 22px;border:1px solid #1a1a18;border-radius:8px;
 background:#1a1a18;color:#fff;cursor:pointer}
button:disabled{opacity:.4;cursor:default}
.hint{margin:14px 0 30px;color:#78756e;font-size:13px;min-height:20px}
.stage{display:grid;grid-template-columns:26px 150px 1fr 78px;gap:14px;
 align-items:center;padding:15px 0;border-top:1px solid #eceae5}
.dot{width:11px;height:11px;border-radius:50%;background:#dcd9d2;
 justify-self:center;transition:background .15s}
.stage.active .dot{background:#1a1a18;animation:pulse 1s infinite}
.stage.done .dot{background:#3f8f5f}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.name b{display:block;font-size:13px}
.name i{font-style:normal;color:#a8a49b;font-size:11px}
.stage.active .name b,.stage.done .name b{color:#1a1a18}
.out{color:#6b6862;font-size:13px;overflow:hidden;text-overflow:ellipsis;
 white-space:nowrap}
.ms{text-align:right;color:#a8a49b;font-size:12px}
.stage.done .ms{color:#3f8f5f}
footer{margin-top:34px;color:#a8a49b;font-size:12px;line-height:1.7}
code{background:#f1efea;padding:1px 5px;border-radius:3px}
</style></head><body><main>
<h1>Voice agent, under the hood</h1>
<p class="sub">Press start, then talk to your machine. Watch where the time goes.</p>
<p class="stack" id="stack"></p>
<button id="go">Start listening</button>
<p class="hint" id="hint">The microphone and speakers are this machine's.</p>

<div id="stages">
  <div class="stage" id="s-vad"><div class="dot"></div>
    <div class="name"><b>Voice activity</b><i>are you still talking?</i></div>
    <div class="out"></div><div class="ms"></div></div>
  <div class="stage" id="s-stt"><div class="dot"></div>
    <div class="name"><b>Speech to text</b><i>audio becomes words</i></div>
    <div class="out"></div><div class="ms"></div></div>
  <div class="stage" id="s-llm"><div class="dot"></div>
    <div class="name"><b>Language model</b><i>words become a reply</i></div>
    <div class="out"></div><div class="ms"></div></div>
  <div class="stage" id="s-tts"><div class="dot"></div>
    <div class="name"><b>Text to speech</b><i>the reply becomes audio</i></div>
    <div class="out"></div><div class="ms"></div></div>
</div>

<footer>
Times are milliseconds since you started speaking. This is the live view; the
authoritative per-stage numbers come from the OpenTelemetry spans
<code>budget.py</code> reads.<br>
Swap a component and run this again: <code>VOICE_TTS_ENGINE=piper make live</code>
</footer>
</main>
<script>
const $ = id => document.getElementById(id);
const el = s => $("s-" + s);
function reset(){ ["vad","stt","llm","tts"].forEach(s=>{
  const e = el(s); e.className = "stage";
  e.querySelector(".out").textContent=""; e.querySelector(".ms").textContent="";
});}
$("go").onclick = async () => {
  $("go").disabled = true;
  $("hint").textContent = "starting the pipeline, this loads models on first run...";
  const r = await (await fetch("/start", {method:"POST"})).json();
  if(!r.ok){ $("hint").textContent = r.error; $("go").disabled = false; }
};
const es = new EventSource("/events");
es.onmessage = m => {
  const e = JSON.parse(m.data);
  if(e.kind === "ready"){ $("stack").textContent = e.text;
    $("hint").textContent = "Listening. Say something."; return; }
  if(e.kind === "reset"){ reset(); return; }
  if(e.kind === "stopped"){ $("go").disabled = false;
    $("hint").textContent = "stopped"; return; }
  if(e.kind === "turn_complete"){ $("hint").textContent =
    "Your turn. Say something else."; return; }
  const box = el(e.stage); if(!box) return;
  if(e.kind === "active"){ box.className = "stage active";
    box.querySelector(".out").textContent = e.text || ""; }
  if(e.kind === "token"){ box.querySelector(".out").textContent += e.text; }
  if(e.kind === "first_token" || e.kind === "first_audio"){
    box.className = "stage active";
    box.querySelector(".out").textContent = e.text || "";
    if(e.ms) box.querySelector(".ms").textContent = Math.round(e.ms)+" ms"; }
  if(e.kind === "done"){ box.className = "stage done";
    if(e.text) box.querySelector(".out").textContent = e.text;
    if(e.ms) box.querySelector(".ms").textContent = Math.round(e.ms)+" ms"; }
};
</script></body></html>"""


def main():
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()

    app = web.Application()
    app.add_routes([web.get("/", index), web.get("/events", sse),
                    web.post("/start", start)])
    logger.info(f"open http://localhost:{args.port}")
    logger.info(f"stack: {factory.describe()}")
    web.run_app(app, port=args.port, print=None)


if __name__ == "__main__":
    main()
