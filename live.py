"""Talk to the agent and watch the latency arrive.

    make live          # then open http://localhost:8080

Click Start, speak into your machine's microphone, and the reply plays through
its speakers. The page draws the turn as a waterfall: what ran, when it started,
how long it took, and what overlapped what.

EVERYTHING ON THAT PAGE IS THE TRACE. The bars are OpenTelemetry spans, streamed
to the browser as Pipecat emits them, positioned by their real start and end
timestamps. They are the same spans `budget.py` reads from
`artifacts/live-traces.jsonl` when the run is over, so the page and the budget
cannot disagree -- there is only one measurement. This page used to map pipeline
frames to bars itself, which was a second implementation of "what happened and
when", and every bug in it came from the two drifting apart.

Two things here are deliberately NOT spans, because they are not timing:
the input level meter, and the reply text as it is generated.

The microphone is the one attached to this machine, not the browser's. That
keeps the whole thing to a local pipeline plus a page of events, with no WebRTC
and no browser audio permissions.
"""

from __future__ import annotations

import asyncio
import json
import time
from functools import partial
from pathlib import Path

from aiohttp import web
from loguru import logger
from pipecat.audio.volume import AudioVolumeTracker
from pipecat.observers.base_observer import BaseObserver, FramePushed

import agent
import budget
import factory
from config import CONFIG
from tracing_setup import init_tracing

UI = Path(__file__).parent / "ui"
TRACES = Path("artifacts/live-traces.jsonl")

# The page draws the stages budget.py reports, under the names budget.py uses.
# Importing them rather than restating them is what keeps the live view and the
# printed budget describing the same four things.
DRAWN = set(budget.STAGES) | {budget.WINDOW}

# One queue PER connected browser. A single shared asyncio.Queue is consumed
# rather than broadcast, so with two tabs open each event would reach only one
# of them and both views would be wrong in different ways.
CLIENTS: list[asyncio.Queue] = []
LOOP: asyncio.AbstractEventLoop | None = None
RUNNING = False
RUNNER = None      # the WorkerRunner, so Stop can cancel it
TASK: asyncio.Task | None = None   # the task running it, so shutdown can join it
SHUTDOWN: asyncio.Event | None = None  # set on ctrl+c, releases the SSE loops


def emit(kind: str, **fields):
    """Fan one event out to every connected browser.

    Every event carries the server's wall clock. Spans are stamped in absolute
    wall-clock nanoseconds, and the browser has no other way to know where
    "now" falls on that axis, so it cannot animate a bar that is still running
    without this.
    """
    ev = {"kind": kind, "now_ns": time.time_ns(), **fields}
    for q in list(CLIENTS):
        try:
            q.put_nowait(ev)
        except asyncio.QueueFull:
            pass


def hello() -> dict:
    """What a browser needs on connect, whenever it happens to connect."""
    return {
        "kind": "hello",
        "now_ns": time.time_ns(),
        "stack": factory.describe(),
        "gate": CONFIG.vad_min_volume,
        "budget_ms": budget.BUDGET_MS,
        "stages": budget.STAGES,
        "window": budget.WINDOW,
        "traces": str(TRACES),
        "running": RUNNING,
    }


def on_span(kind: str, span: dict):
    """A span started or ended. Send it, from whichever thread produced it.

    Span callbacks run wherever the work finished, which for a local model is a
    worker thread, not the event loop. Touching the client queues directly from
    there is a data race; this hands the event over instead.
    """
    if span["name"] not in DRAWN or LOOP is None:
        return
    try:
        LOOP.call_soon_threadsafe(partial(emit, kind, span=span))
    except RuntimeError:
        pass       # loop already closed: shutting down


class PageObserver(BaseObserver):
    """The two things on the page that are not timing.

    Deliberately small, and deliberately NOT a source of any number. Everything
    with a millisecond on it comes from the spans. This reports the input level
    and the reply text, neither of which any span carries.

    Subclasses BaseObserver DIRECTLY. Mixing it in behind BaseObserver puts
    BaseObserver first in the MRO, so its no-op `on_push_frame` wins and this
    never runs: the pipeline links, reports ready, and observes nothing. That
    silently disabled this whole page once.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._lvl_at = 0.0                  # throttle for the input meter
        self._vol = AudioVolumeTracker()    # the same metric the VAD gates on

    async def on_push_frame(self, data: FramePushed):
        from pipecat.frames.frames import (
            BotStoppedSpeakingFrame,
            InputAudioRawFrame,
            LLMTextFrame,
            TranscriptionFrame,
            UserStartedSpeakingFrame,
        )

        f = data.frame

        # Input level, ~10/s. Without this a silent pipeline is indistinguishable
        # from a wrong microphone or a VAD gate the signal never reaches, which
        # is exactly the failure this page is supposed to make obvious.
        if isinstance(f, InputAudioRawFrame):
            # Measured with Pipecat's OWN volume function, not a hand-rolled
            # RMS. The VAD compares this exact number against min_volume, so
            # the meter and the gate marker on the page are the same scale and
            # "am I loud enough?" is answerable by looking.
            self._vol.update(f.audio, f.sample_rate)
            now = time.monotonic()
            if now - self._lvl_at > 0.1:
                self._lvl_at = now
                emit("level", v=self._vol.volume)
            return

        if isinstance(f, UserStartedSpeakingFrame):
            emit("turn_start")
        elif isinstance(f, TranscriptionFrame) and f.text:
            emit("heard", text=f.text)
        elif isinstance(f, LLMTextFrame) and f.text:
            emit("token", text=f.text)
        elif isinstance(f, BotStoppedSpeakingFrame):
            emit("turn_done")


async def run_agent():
    """One live conversation, using this machine's microphone and speakers."""
    global RUNNING, RUNNER

    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )
    from pipecat.workers.runner import WorkerRunner

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=CONFIG.input_sample_rate,
            audio_out_sample_rate=CONFIG.output_sample_rate,
            input_device_index=CONFIG.audio_device,
        )
    )
    devs = factory.list_input_devices()
    cur = CONFIG.audio_device
    name = next((d["name"] for d in devs
                 if d["index"] == cur or (cur is None and d["default"])), "?")
    logger.info(f"microphone: [{cur if cur is not None else 'default'}] {name}")
    emit("devices", devices=devs, current=cur, name=name)

    # The pipeline is agent.py's, not a second copy of it. A live turn and a
    # WAV turn go through exactly the same stages, in the same order, under the
    # same endpointing policy.
    pipeline = agent.build_pipeline(transport, mute_while_bot_speaks=True)
    worker = agent.build_worker(pipeline, mode="live",
                                conversation_id=f"live-{int(time.time())}",
                                observers=[PageObserver()])

    runner = WorkerRunner(handle_sigint=False)
    RUNNER = runner
    await runner.add_workers(worker)

    emit("ready", stack=factory.describe())
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
        await resp.write(f"data: {json.dumps(hello())}\n\n".encode())
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
    return web.FileResponse(UI / "index.html")


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
        global SHUTDOWN, LOOP
        SHUTDOWN = asyncio.Event()
        LOOP = asyncio.get_running_loop()

    # Truncated per run. Spans append, and a file holding four sessions makes
    # `budget.py --traces` report a median across conversations you have
    # forgotten having.
    TRACES.unlink(missing_ok=True)
    ok, _ = init_tracing("voice-live", TRACES, live_callback=on_span)

    app = web.Application()
    app.on_startup.append(boot)
    app.on_shutdown.append(release_clients)
    app.on_cleanup.append(shutdown)
    app.add_routes([web.get("/", index), web.get("/events", sse),
                    web.post("/start", start), web.post("/stop", stop),
                    web.static("/ui", UI)])
    logger.info(f"open http://localhost:{args.port}")
    logger.info(f"stack: {factory.describe()}")
    logger.info(f"tracing: {ok}, spans -> {TRACES}")
    logger.info(f"afterwards: uv run python budget.py --traces {TRACES}")
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
