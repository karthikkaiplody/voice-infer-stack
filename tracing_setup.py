"""OpenTelemetry wiring: spans to a JSONL file, and to the live page.

Jaeger is deliberately not used. Requiring docker to see where the time went
would defeat the point of a clone-and-run repo, so spans land in a file that
`budget.py` and `viewer.py` read with nothing installed.

The same spans drive the live page. That is the point of `LiveSpanProcessor`:
the browser is not shown a second, hand-maintained version of what happened, it
is shown the trace itself as it is produced. One source of truth, two readers.
"""

import json
import threading
import time
from collections import deque
from pathlib import Path

from loguru import logger
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from pipecat.frames.frames import (
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection
from pipecat.utils.tracing.setup import setup_tracing

TRACER = "voice-infer-stack"


def span_as_dict(span) -> dict:
    """One span as the JSON object SPANS.md describes.

    Works on a span that has not ended yet, where `end_time` is None. The live
    page needs exactly that: a bar it can start drawing while the stage runs.
    """
    ctx = span.get_span_context()
    parent = span.parent
    end = span.end_time
    return {
        "name": span.name,
        "trace_id": f"{ctx.trace_id:032x}",
        "span_id": f"{ctx.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent else None,
        # Wall-clock ns. Interval arithmetic (not duration sums) is what
        # proves overlap, so both endpoints are preserved.
        "start_time_ns": span.start_time,
        "end_time_ns": end,
        "duration_ms": (end - span.start_time) / 1e6 if end else None,
        "attributes": {k: v for k, v in (span.attributes or {}).items()},
    }


class JsonlSpanExporter(SpanExporter):
    """Appends one JSON object per span. Timestamps kept in ns, unmodified."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans) -> SpanExportResult:
        lines = [json.dumps(span_as_dict(s)) for s in spans]
        with self._lock:
            with self._path.open("a") as f:
                f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


class LiveSpanProcessor(SpanProcessor):
    """Hand every span to a callback the moment it starts and the moment it ends.

    The JSONL exporter sits behind a BatchSpanProcessor, which is right for a
    file and useless for a page that should light up while the stage is still
    running. This one is synchronous and unbuffered.

    `on_start` carries a span whose `end_time` is None and whose attributes are
    mostly still unset: enough to place the left edge of a bar, not enough to
    label it. `on_end` carries the finished article.

    The callback runs on whatever thread ended the span, which for a local
    model is usually not the event loop. Anything it touches must be safe there;
    `live.py` hands straight over with `call_soon_threadsafe`.
    """

    def __init__(self, callback):
        self._callback = callback

    def on_start(self, span, parent_context=None):
        self._safely("span_start", span)

    def on_end(self, span: ReadableSpan):
        self._safely("span_end", span)

    def _safely(self, kind, span):
        """A broken page must not cost you the trace.

        Span processors run inside Pipecat's own tracing path, which catches
        the exception, logs it at WARNING, and gives up on the span. One
        TypeError here and `stt`, `llm` and `tts` quietly stop being traced at
        all -- in the file as well as on the page.
        """
        try:
            self._callback(kind, span_as_dict(span))
        except Exception as e:
            logger.warning(f"live span callback failed: {e}")

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def init_tracing(service_name: str, jsonl_path: str | Path,
                 live_callback=None, console: bool = False):
    """Start tracing. Spans go to the file, and optionally to a live callback."""
    exporter = JsonlSpanExporter(jsonl_path)
    ok = setup_tracing(service_name=service_name, exporter=exporter,
                       console_export=console)
    if ok and live_callback is not None:
        trace.get_tracer_provider().add_span_processor(
            LiveSpanProcessor(live_callback))
    return ok, exporter


class TurnSpanObserver(BaseObserver):
    """The two spans Pipecat does not emit, on Pipecat's own clock.

    Pipecat traces `stt`, `llm` and `tts` and wraps them in `turn` and
    `conversation`. It does not trace the silence it sits through before
    deciding your turn is over, and it has no span for the number this repo is
    about: from the moment you stop making noise to the moment the first
    synthesized sample exists. Without the first, endpointing is invisible
    because it hides inside an STT span that began while you were still
    talking. Without the second there is no defensible definition of latency.

    Nothing here reads a clock to decide when speech ended.
    `VADUserStoppedSpeakingFrame` carries both the moment the VAD made its
    call and the silence it had to hear first, so the end of speech is
    `timestamp - stop_secs` -- Pipecat's own arithmetic, not a guess from
    config. It is also why a mid-sentence pause cannot corrupt the number: if
    you resume, the next `VADUserStartedSpeakingFrame` discards the candidate.

    Subclasses BaseObserver DIRECTLY, and every observer here must. Mixing it
    in behind BaseObserver puts BaseObserver first in the MRO, so its no-op
    `on_push_frame` wins, the pipeline reports healthy, and nothing is ever
    observed.
    """

    def __init__(self, *, mode: str, fixture: str | None = None,
                 max_frames: int = 200, **kwargs):
        super().__init__(**kwargs)
        self._mode = mode
        self._fixture = fixture
        self._speech_end: float | None = None   # wall clock seconds
        self._stop_secs: float = 0.0
        self._window_open = False
        self._seen: set = set()
        self._history: deque = deque(maxlen=max_frames)

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        # The same frame is pushed at every hop in the pipeline. Without this
        # a turn emits one span per processor.
        if data.frame.id in self._seen:
            return
        self._seen.add(data.frame.id)
        self._history.append(data.frame.id)
        if len(self._seen) > len(self._history):
            self._seen = set(self._history)

        f = data.frame
        if isinstance(f, VADUserStartedSpeakingFrame):
            self._speech_end = None
        elif isinstance(f, VADUserStoppedSpeakingFrame):
            self._speech_end = f.timestamp - f.stop_secs
            self._stop_secs = f.stop_secs
        elif isinstance(f, UserStoppedSpeakingFrame):
            self._close_turn_detection()
        elif isinstance(f, TTSAudioRawFrame):
            self._open_and_close_window()

    def _close_turn_detection(self):
        """The wait is over: the pipeline has accepted that you stopped talking."""
        if self._speech_end is None:
            return
        tracer = trace.get_tracer(TRACER)
        span = tracer.start_span("turn_detection",
                                 start_time=int(self._speech_end * 1e9))
        end = time.time_ns()
        span.set_attribute("strategy", "vad_timeout")
        span.set_attribute("vad.stop_secs", self._stop_secs)
        span.set_attribute("wait_ms",
                           round(end / 1e6 - self._speech_end * 1000, 1))
        # How the moment was observed, kept on the span so a row is never
        # silently treated as a like-for-like measurement of a different one.
        span.set_attribute("measured_as", "vad_frame_timestamp")
        span.end(end_time=end)
        self._window_open = True

    def _open_and_close_window(self):
        """First synthesized sample. Write the budget window, start to finish.

        Written in one go, backdated to the end of speech, rather than opened
        early and closed here. A span that is opened and never ended is never
        exported, so a turn the agent failed to answer would leave the file
        holding a window that silently never appears -- and the page waiting on
        a bar that never arrives.
        """
        if not self._window_open or self._speech_end is None:
            return
        self._window_open = False
        tracer = trace.get_tracer(TRACER)
        span = tracer.start_span("e2e.speech_end_to_first_audio",
                                 start_time=int(self._speech_end * 1e9))
        end = time.time_ns()
        span.set_attribute("mode", self._mode)
        if self._fixture:
            span.set_attribute("fixture", self._fixture)
        try:
            import factory
            span.set_attribute("stack", factory.describe())
        except Exception:
            pass
        span.set_attribute("duration_ms",
                           round(end / 1e6 - self._speech_end * 1000, 1))
        span.end(end_time=end)
