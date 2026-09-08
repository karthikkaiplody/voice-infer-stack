"""OpenTelemetry wiring: spans to a JSONL file, no collector required.

Jaeger is deliberately not used. A three-fixture single-process benchmark does
not need a trace UI, and requiring docker to reproduce the numbers would defeat
the point of a clone-and-run repo. Spans land in a file that chart.py reads.
"""

import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from pipecat.frames.frames import UserStoppedSpeakingFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.utils.tracing.setup import setup_tracing


class JsonlSpanExporter(SpanExporter):
    """Appends one JSON object per span. Timestamps kept in ns, unmodified."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans) -> SpanExportResult:
        lines = []
        for span in spans:
            lines.append(json.dumps(self._as_dict(span)))
        with self._lock:
            with self._path.open("a") as f:
                f.write("\n".join(lines) + "\n")
        return SpanExportResult.SUCCESS

    @staticmethod
    def _as_dict(span: ReadableSpan) -> dict:
        ctx = span.get_span_context()
        parent = span.parent
        return {
            "name": span.name,
            "trace_id": f"{ctx.trace_id:032x}",
            "span_id": f"{ctx.span_id:016x}",
            "parent_span_id": f"{parent.span_id:016x}" if parent else None,
            # Wall-clock ns. Interval arithmetic (not duration sums) is what
            # proves overlap, so both endpoints are preserved.
            "start_time_ns": span.start_time,
            "end_time_ns": span.end_time,
            "duration_ms": (span.end_time - span.start_time) / 1e6,
            "attributes": {k: v for k, v in (span.attributes or {}).items()},
        }

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def init_tracing(service_name: str, jsonl_path: str | Path, console: bool = False):
    exporter = JsonlSpanExporter(jsonl_path)
    ok = setup_tracing(service_name=service_name, exporter=exporter, console_export=console)
    return ok, exporter


@contextmanager
def stage_span(name: str, **attrs):
    """Emit one stage span using the same names and attribute keys Pipecat uses.

    The sequential build does not run inside Pipecat, so it has to emit its own
    spans. Matching Pipecat's vocabulary exactly (`stt`, `llm`, `tts`, and the
    OpenTelemetry `gen_ai.*` conventions) is what lets one analyser read traces
    from both builds, and lets anyone else's OTel-instrumented agent be read by
    the same code. See SPANS.md.
    """
    tracer = trace.get_tracer("voice-infer-stack")
    with tracer.start_as_current_span(name) as span:
        for key, value in attrs.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


def emit_e2e_span(capture, mode: str, fixture: str):
    """Emit the span that defines the latency budget window.

    Everything before `t_speech_end` is the user talking, which costs the user
    nothing. The budget starts the instant they stop and ends when the first
    synthesized sample exists. Stage spans get clipped to this window by
    budget.py, otherwise a stage that began while the user was still speaking
    (segmented STT does exactly that) looks like it dominates the latency when
    most of its span was free.

    The span is written with EXPLICIT start and end timestamps converted from
    the capture's monotonic clock via `Capture.to_wall_ns`, so it lands on the
    same wall-clock timeline as every Pipecat-emitted span.
    """
    if capture.t_speech_end is None or capture.t_first_audio is None:
        return
    tracer = trace.get_tracer("voice-infer-stack")
    span = tracer.start_span(
        "e2e.speech_end_to_first_audio",
        start_time=capture.to_wall_ns(capture.t_speech_end),
    )
    span.set_attribute("mode", mode)
    span.set_attribute("fixture", fixture)
    span.set_attribute(
        "duration_ms", round(capture.e2e_speech_end_to_first_audio * 1000, 1)
    )
    span.end(end_time=capture.to_wall_ns(capture.t_first_audio))


class TurnEndObserver(BaseObserver):
    """Timestamp the moment Pipecat decides the user's turn is over.

    Pipecat traces `stt`, `llm` and `tts`, but emits no span for endpointing, so
    the silence wait is invisible: it hides inside the STT span, which starts
    while the user is still talking. Without this the streaming build appears to
    have no turn-detection cost at all, and the two builds cannot be compared
    stage by stage.

    Deriving the wait from `stop_secs` instead would be asserting the answer.
    This measures it.
    """

    def __init__(self, capture, **kwargs):
        super().__init__(**kwargs)
        self._capture = capture

    async def on_push_frame(self, data: FramePushed):
        if isinstance(data.frame, UserStoppedSpeakingFrame):
            if self._capture.t_turn_end is None:
                self._capture.t_turn_end = time.monotonic()


def emit_turn_detection_span(capture, strategy: str, stop_secs: float,
                             measured_as: str = "unknown"):
    """The silence wait, as its own span, on the shared wall clock.

    `measured_as` records HOW the moment was observed, because the two builds
    cannot observe it the same way: naive reads the VAD state transition
    directly, streaming sees UserStoppedSpeakingFrame travel the pipeline. With
    endpointing policy matched they agree closely, but the provenance is kept on
    the span so the rows are never silently treated as identical measurements.
    """
    if capture.t_speech_end is None or capture.t_turn_end is None:
        return
    tracer = trace.get_tracer("voice-infer-stack")
    span = tracer.start_span(
        "turn_detection", start_time=capture.to_wall_ns(capture.t_speech_end)
    )
    span.set_attribute("strategy", strategy)
    span.set_attribute("vad.stop_secs", stop_secs)
    span.set_attribute("wait_ms", round(capture.turn_detection_wait * 1000, 1))
    span.set_attribute("measured_as", measured_as)
    span.end(end_time=capture.to_wall_ns(capture.t_turn_end))
