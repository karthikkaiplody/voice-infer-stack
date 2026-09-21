"""The local server behind the observability page.

    make demo          # open on safe synthetic fixtures, no microphone or model
    make live          # open on the live agent; then http://127.0.0.1:8080

Two modes share one page and one event stream. `--mode` picks the one to start in,
and the page has a switch for the other (`POST /mode/<mode>`):

* `--mode fixture` replays the repository's synthetic contract-v1 fixtures. It
  needs no microphone, provider, API key or model, and does not import Pipecat
  until the page switches to live.
* `--mode live` runs the real agent on this machine's microphone and speakers,
  and streams the contract-v1 events its Pipecat runtime actually produced.

The browser receives only contract-v1 telemetry events (metadata-only, already
through `telemetry/contract.py`'s privacy boundary), a safe configuration summary, and a
few fixed status messages. Nothing on that stream carries audio, transcripts,
prompts, replies, tool data, raw errors, device names or paths.

A stage the running source cannot report is listed in `capabilities` and shown
as "Not instrumented". This server never estimates a stage it did not observe.

The microphone is the one attached to this machine, not the browser's. That
keeps the whole thing to a local pipeline plus a page of events, with no WebRTC
and no browser audio permissions.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import time
from functools import partial
from pathlib import Path

from aiohttp import web
from loguru import logger

from voice_agent import paths
from voice_agent.knowledge import agents
from voice_agent.pipeline import factory
from voice_agent.server import tuning
from voice_agent.config import CONFIG
from voice_agent.telemetry.runtime_events import SpanEventBridge
from voice_agent.telemetry.contract import (
    RuntimeEventEmitter,
    filter_metadata,
    is_safe_identifier,
    load_jsonl,
    validate_trace,
)

UI = paths.UI_DIST
TRACES = paths.ARTIFACTS_DIR / "live-traces.jsonl"

# One queue PER connected browser. A single shared asyncio.Queue is consumed
# rather than broadcast, so with two tabs open each event would reach only one
# of them and both views would be wrong in different ways.
CLIENTS: list[asyncio.Queue] = []
LOOP: asyncio.AbstractEventLoop | None = None
RUNNING = False
RUNNER = None      # the WorkerRunner, so Stop can cancel it
TASK: asyncio.Task | None = None   # the task running it, so shutdown can join it
SHUTDOWN: asyncio.Event | None = None  # set on ctrl+c, releases the SSE loops
EVENT_EMITTER: RuntimeEventEmitter | None = None
SPAN_BRIDGE: SpanEventBridge | None = None
SERVER_MODE = "live"
TRACING_STARTED = False
# The configuration this process launched with, and the one the next Start will
# build the pipeline from. They differ only after the page saves tuning changes.
BASE_CONFIG = CONFIG
ACTIVE_CONFIG = CONFIG
REPLAY_TASK: asyncio.Task | None = None

SCENARIOS = {
    "normal-completed": "normal-completed.jsonl",
    "slow-blocking-tool": "slow-blocking-tool.jsonl",
    "interrupted": "interrupted.jsonl",
    "false-endpoint": "false-endpoint.jsonl",
    "failed-tool": "failed-tool.jsonl",
    "grounded-answer": "grounded-answer.jsonl",
}
FIXTURE_DIR = paths.TELEMETRY_FIXTURES_DIR

# Replay is accelerated: the wait between events is capped so a 2-second tool
# call does not make someone stare at a static page. The events keep their
# recorded timestamps, and the page measures from those, not from arrival.
REPLAY_MAX_GAP_S = 0.35

STAGES = ("user_speech", "endpointing", "stt", "retrieval", "llm", "tools", "tts",
          "output_transport")
# What the live Pipecat runtime cannot truthfully report today. The agent
# registers no tools. Real interruption is fixture-only: the live pipeline mutes
# the microphone while the bot speaks (see agent.build_pipeline).
LIVE_NOT_INSTRUMENTED_STAGES = frozenset({"tools"})
LIVE_NOT_INSTRUMENTED_OUTCOMES = frozenset({"interrupted"})


def broadcast(message: dict):
    """Fan one message out to every connected browser."""
    for q in list(CLIENTS):
        try:
            q.put_nowait(message)
        except asyncio.QueueFull:
            pass


def emit(kind: str, **fields):
    broadcast({"kind": kind, "now_ns": time.time_ns(), **fields})


def capabilities() -> dict:
    """What this source can report, so the page can say what it cannot."""
    if SERVER_MODE == "fixture":
        return {"stages": {stage: True for stage in STAGES},
                "outcomes": {"completed": True, "interrupted": True,
                             "failed": True}}
    # Retrieval is a stage only when the active agent has knowledge to search
    # (see agent.build_pipeline); otherwise it is honestly not instrumented.
    active_agent = agents.agent_for(ACTIVE_CONFIG)
    not_instrumented = LIVE_NOT_INSTRUMENTED_STAGES
    if active_agent is None or not active_agent.has_knowledge:
        not_instrumented = not_instrumented | {"retrieval"}
    return {
        "stages": {stage: stage not in not_instrumented for stage in STAGES},
        "outcomes": {outcome: outcome not in LIVE_NOT_INSTRUMENTED_OUTCOMES
                     for outcome in ("completed", "interrupted", "failed")},
    }


def agent_summary() -> dict | None:
    """Which agent is answering, for the page's header.

    Only the live source has one: the fixtures are synthetic. The id and title are
    written in this repository (`agents/<id>/agent.toml`), never taken from a caller.
    """
    if SERVER_MODE != "live":
        return None
    definition = agents.agent_for(ACTIVE_CONFIG)
    if definition is None:
        return None
    return {"id": definition.id, "title": definition.title}


def hello() -> dict:
    """What a browser needs on connect, whenever it happens to connect."""
    fixture = SERVER_MODE == "fixture"
    message = {
        "kind": "hello",
        "now_ns": time.time_ns(),
        "mode": SERVER_MODE,
        "configuration": safe_configuration(),
        "capabilities": capabilities(),
        "agent": agent_summary(),
        "scenarios": list(SCENARIOS) if fixture else [],
        "running": RUNNING,
    }
    if fixture:
        message["replay"] = {"pacing": "accelerated",
                             "max_gap_ms": int(REPLAY_MAX_GAP_S * 1000)}
    else:
        message["tuning"] = tuning_state()
    return message


def tuning_state() -> dict:
    """The tunable settings with their limits, launch values and current values."""
    return {"fields": tuning.describe(BASE_CONFIG, ACTIVE_CONFIG)}


_CONFIG_KEYS = ("snapshot_id", "source_revision", "provider_ids", "model_ids",
                "endpointing_settings", "hardware_class", "cpu_architecture",
                "os_version", "execution_engine_id", "privacy_mode")
_MAP_KEYS = frozenset({"provider_ids", "model_ids", "endpointing_settings"})


def _safe_scalar(value, *, model: bool = False):
    """A scalar the browser may be shown, or None. Strings must be safe IDs."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if not isinstance(value, str):
        return None
    if model:
        return value if filter_metadata({"param.model": value}) else None
    return value if is_safe_identifier(value) else None


def export_configuration(raw: dict) -> dict:
    """The configuration summary as the browser sees it: allowlisted keys, and
    only safe scalar values. Anything else is dropped, not shown."""
    clean: dict = {}
    for key in _CONFIG_KEYS:
        value = raw.get(key)
        if key in _MAP_KEYS:
            if isinstance(value, dict):
                clean[key] = {
                    str(name): scalar for name, item in value.items()
                    if (scalar := _safe_scalar(item, model=key != "endpointing_settings"))
                    is not None and is_safe_identifier(str(name))}
        elif key == "privacy_mode":
            clean[key] = "metadata-only"
        else:
            clean[key] = _safe_scalar(value)
    return clean


def safe_configuration() -> dict:
    if SERVER_MODE == "fixture":
        with (FIXTURE_DIR / "configuration.json").open(encoding="utf-8") as handle:
            return export_configuration(json.load(handle))
    from voice_agent.pipeline import agent   # Pipecat: only the live path pays for it

    return export_configuration(agent.snapshot_for(ACTIVE_CONFIG).as_dict())


def browser_stack_description() -> str:
    """A browser-safe description built only from validated identifiers."""
    try:
        selected = factory.component_identity()
    except (Exception, SystemExit):
        return "configuration_invalid"
    keyed = {
        "settings.engine": selected["stt_engine"],
        "settings.model": selected["stt_model"],
        "gen_ai.provider.name": selected["llm_provider"],
        "gen_ai.request.model": selected["llm_model"],
        "voice_id": selected["tts_voice"],
    }
    if filter_metadata(keyed) != keyed:
        return "configuration_invalid"
    return (f"stt={selected['stt_engine']}:{selected['stt_model']}  "
            f"llm={selected['llm_provider']}:{selected['llm_model']}  "
            f"tts={selected['tts_engine']}:{selected['tts_voice']}  "
            f"endpoint={factory.endpointing_strategy()}")


def browser_error_classification(error: BaseException) -> str:
    """Map exceptions to a fixed browser-safe classification."""
    if isinstance(error, asyncio.CancelledError):
        classification = "cancelled"
    elif isinstance(error, (ValueError, TypeError)):
        classification = "validation_error"
    elif isinstance(error, (OSError, TimeoutError)):
        classification = "transport_error"
    else:
        classification = "pipeline_error"
    return filter_metadata({
        "error.classification": classification,
    }).get("error.classification", "internal_error")


def on_span(kind: str, span: dict):
    """A Pipecat span started or ended, from whichever thread produced it.

    Span callbacks run wherever the work finished, which for a local model is a
    worker thread, not the event loop. The bridge and the emitter are
    thread-safe and hand each accepted event over with `call_soon_threadsafe`.
    """
    bridge = SPAN_BRIDGE
    if bridge is not None:
        bridge.on_span(kind, span)


def on_input_silence(silent: bool):
    """The microphone has started, or stopped, delivering only silence."""
    emit("input_silent" if silent else "input_ok")


def on_event(event: dict):
    """An accepted contract-v1 event, from any thread, on its way to browsers."""
    if LOOP is None:
        return
    try:
        LOOP.call_soon_threadsafe(partial(emit, "telemetry", event=event))
    except RuntimeError:
        pass       # loop already closed: shutting down


async def run_agent():
    """One live conversation, using this machine's microphone and speakers."""
    global RUNNING, RUNNER, EVENT_EMITTER, SPAN_BRIDGE

    config = ACTIVE_CONFIG          # fixed for this run; changes apply to the next
    from voice_agent.pipeline import agent   # Pipecat: only the live path pays for it
    from voice_agent.server.input_watch import InputSilenceObserver
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )
    from pipecat.workers.runner import WorkerRunner

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=config.input_sample_rate,
            audio_out_sample_rate=config.output_sample_rate,
            input_device_index=config.audio_device,
        )
    )
    source = "default" if config.audio_device is None else "configured index"
    logger.info(f"microphone input: {source}")

    # The pipeline is agent.py's, not a second copy of it. A live turn and a
    # WAV turn go through exactly the same stages, in the same order, under the
    # same endpointing policy.
    pipeline = agent.build_pipeline(transport, mute_while_bot_speaks=True,
                                    config=config)
    worker = agent.build_worker(pipeline, mode="live",
                                conversation_id=f"live-{int(time.time())}",
                                event_callback=on_event, config=config,
                                observers=(InputSilenceObserver(on_input_silence),))
    EVENT_EMITTER = worker.voice_event_emitter
    SPAN_BRIDGE = SpanEventBridge(EVENT_EMITTER)

    # The agent greets first, if it has a greeting. Queued now; Pipecat sends it
    # through the pipeline as soon as the pipeline has started.
    await worker.queue_frames(agent.greeting_frames(config))

    runner = WorkerRunner(handle_sigint=False)
    RUNNER = runner
    await runner.add_workers(worker)

    emit("ready")
    try:
        await runner.run()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.exception("pipeline failed")
        emit("error", classification=browser_error_classification(e))
    finally:
        RUNNING = False
        RUNNER = None
        EVENT_EMITTER = None
        SPAN_BRIDGE = None
        emit("stopped")


async def replay(request):
    """Replay one synthetic fixture on the event stream, accelerated."""
    global REPLAY_TASK
    if SERVER_MODE != "fixture":
        return web.json_response(
            {"ok": False, "error": "fixture_mode_required"}, status=409)
    scenario = request.match_info["scenario"]
    filename = SCENARIOS.get(scenario)
    if filename is None:
        return web.json_response(
            {"ok": False, "error": "unknown_scenario"}, status=404)
    events = load_jsonl(FIXTURE_DIR / filename)
    validate_trace(events)
    if REPLAY_TASK is not None and not REPLAY_TASK.done():
        REPLAY_TASK.cancel()

    async def play():
        emit("replay_reset", scenario=scenario)
        previous = events[0]["timestamp_ns"]
        for event in events:
            delay = min((event["timestamp_ns"] - previous) / 1e9,
                        REPLAY_MAX_GAP_S)
            if delay > 0:
                await asyncio.sleep(delay)
            emit("telemetry", event=event)
            previous = event["timestamp_ns"]
        emit("replay_finished", scenario=scenario)

    REPLAY_TASK = asyncio.create_task(play())
    return web.json_response({"ok": True, "scenario": scenario})


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
    if SERVER_MODE != "live":
        return web.json_response(
            {"ok": False, "error": "live_mode_required"}, status=409)
    if RUNNING:
        return web.json_response({"ok": False, "error": "already running"})
    RUNNING = True
    # Held, not fire-and-forget. An untracked task keeps the event loop alive
    # after aiohttp has shut down, so ctrl+c appeared to do nothing.
    TASK = asyncio.create_task(run_agent())
    return web.json_response({"ok": True})


MAX_CONFIG_BODY = 4096


async def configure(request):
    """Save tuning changes. They take effect the next time the pipeline starts.

    The body is `{"values": {...}}`: the settings to differ from the launch
    configuration. An empty object restores the launch values.
    """
    global ACTIVE_CONFIG
    if SERVER_MODE != "live":
        return web.json_response(
            {"ok": False, "error": "live_mode_required"}, status=409)
    if RUNNING:
        return web.json_response({"ok": False, "error": "stop_first"}, status=409)
    if request.content_length is None or request.content_length > MAX_CONFIG_BODY:
        return web.json_response({"ok": False, "error": "invalid_body"}, status=413)
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(body, dict) or set(body) != {"values"}:
        return web.json_response({"ok": False, "error": "invalid_body"}, status=400)
    try:
        ACTIVE_CONFIG = tuning.apply(BASE_CONFIG, body["values"])
    except tuning.TuningError as error:
        return web.json_response({"ok": False, "error": error.code}, status=400)
    emit("configuration", configuration=safe_configuration(),
         tuning=tuning_state())
    return web.json_response({"ok": True})


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
    page = UI / "index.html"
    if not page.is_file():
        return web.Response(
            status=503, content_type="text/plain",
            text="The UI is not built yet. Run: make ui-build\n")
    return web.FileResponse(page)


@web.middleware
async def local_control_only(request, handler):
    """Reject anything not addressed to this machine's loopback address.

    Applies to every route, reads included: the event stream is what would leak
    to a page reaching this server through a rebound DNS name, and the controls
    are what a cross-origin form post would trigger. A browser reaching the
    server as `127.0.0.1` sends that Host and, when it sends Origin at all, the
    same origin.
    """
    host = request.headers.get("Host", "")
    allowed_host = host == "127.0.0.1" or host.startswith("127.0.0.1:")
    origin = request.headers.get("Origin")
    allowed_origin = origin is None or origin == f"http://{host}"
    if not allowed_host or not allowed_origin:
        return web.json_response(
            {"ok": False, "error": "local_request_required"}, status=403)
    return await handler(request)


async def boot(app):
    global SHUTDOWN, LOOP
    SHUTDOWN = asyncio.Event()
    LOOP = asyncio.get_running_loop()


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
    tidy exit here is what makes `make demo-live` restartable.
    """
    global TASK
    if REPLAY_TASK is not None and not REPLAY_TASK.done():
        REPLAY_TASK.cancel()
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


def start_tracing():
    """Live mode's span file. Started once, the first time live mode is in use."""
    global TRACING_STARTED
    if TRACING_STARTED:
        return
    TRACING_STARTED = True
    from voice_agent.telemetry.tracing import init_tracing

    # Truncated per run. Spans append, and a file holding four sessions
    # makes `budget.py --traces` report a median across conversations you
    # have forgotten having.
    TRACES.unlink(missing_ok=True)
    ok, _ = init_tracing("voice-live", TRACES, live_callback=on_span)
    logger.info(f"stack: {factory.describe()}")
    logger.info(f"tracing: {ok}, spans -> {TRACES}")
    logger.info(f"afterwards: uv run python -m voice_agent.analysis.budget --traces {TRACES}")


async def set_mode(request):
    """Switch the page between recorded replays and the live agent.

    Every connected page is sent a fresh `hello`, which is what a page already does
    on connecting: it drops what it was showing and shows what the server says now.
    Refused while the live pipeline is running, so the microphone is never left
    running behind a page that no longer shows it.
    """
    global SERVER_MODE
    mode = request.match_info["mode"]
    if mode not in ("fixture", "live"):
        return web.json_response({"ok": False, "error": "unknown_mode"}, status=404)
    if RUNNING:
        return web.json_response({"ok": False, "error": "stop_first"}, status=409)
    if mode != SERVER_MODE:
        if REPLAY_TASK is not None and not REPLAY_TASK.done():
            REPLAY_TASK.cancel()
        SERVER_MODE = mode
        if mode == "live":
            start_tracing()      # imports Pipecat: the first switch takes a moment
        broadcast(hello())
    return web.json_response({"ok": True, "mode": SERVER_MODE})


def create_app() -> web.Application:
    app = web.Application(middlewares=[local_control_only])
    app.on_startup.append(boot)
    app.on_shutdown.append(release_clients)
    app.on_cleanup.append(shutdown)
    routes = [web.get("/", index), web.get("/events", sse),
              web.post("/start", start), web.post("/stop", stop),
              web.post("/config", configure),
              web.post("/mode/{mode}", set_mode),
              web.post("/replay/{scenario}", replay)]
    if (UI / "assets").is_dir():
        routes.append(web.static("/assets", UI / "assets"))
    app.add_routes(routes)
    return app


def configure_logging():
    """Console logging at INFO.

    Pipecat logs speech-to-text transcripts and the words it is about to speak at
    DEBUG, and loguru prints DEBUG by default. The page and the trace file carry
    metadata only, so the terminal must not be the one place spoken and generated
    text shows up (a terminal is easily copied, recorded or redirected to a file).
    """
    logger.remove()
    logger.add(sys.stderr, level="INFO")


def main():
    global SERVER_MODE
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--mode", choices=("fixture", "live"), default="live")
    args = ap.parse_args()
    configure_logging()
    SERVER_MODE = args.mode

    host = "127.0.0.1"
    logger.info(f"mode: {SERVER_MODE}")
    if not (UI / "index.html").is_file():
        logger.warning("ui/dist is missing: run `make ui-build` first")
    if SERVER_MODE == "live":
        start_tracing()
    logger.info(f"open http://{host}:{args.port}")
    logger.info("ctrl+c to quit")
    from aiohttp.web_runner import GracefulExit

    try:
        web.run_app(create_app(), host=host, port=args.port, print=None,
                    handle_signals=True)
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
