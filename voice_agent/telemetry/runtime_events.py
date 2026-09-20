"""Turn Pipecat's safe spans into contract-v1 events for the local browser.

`TurnSpanObserver` sees frames and owns the boundaries Pipecat does not trace
(speech, endpointing, synthesized sample, transport acceptance, outcome). The
`stt`, `llm` and `tts` stages come from the spans Pipecat itself emits. This
module maps those spans, and only those, onto the same events.

Rules, all of which keep the browser from being shown a number nobody measured:

* An event is only emitted from a timestamp that a span carries, or from a
  measured duration added to a timestamp that a span or frame carries.
* If a measurement is missing (no `metrics.ttfb`, a negative or non-finite
  one), the event is not emitted. It is counted in `skipped`, never estimated.
* `stt.first` is measured from the end of speech, because that is where
  Pipecat starts the STT time-to-first-byte clock. It is therefore only
  emitted once `user_speech.ended` is known, and waits for it until then.

Span callbacks run on whichever thread ended the span, so everything here runs
under the emitter's lock, and the emitter never raises.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from voice_agent.telemetry.contract import RuntimeEventEmitter

_NS = 1_000_000_000


def _ttfb_ns(attributes: Mapping[str, Any]) -> int | None:
    """Pipecat's measured time to first byte, in ns, if it is a sane number."""
    value = attributes.get("metrics.ttfb")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value * _NS)


class SpanEventBridge:
    """Feed `stt` / `llm` / `tts` span callbacks into a `RuntimeEventEmitter`."""

    def __init__(self, emitter: RuntimeEventEmitter):
        self._emitter = emitter
        # The emitter's own lock. A second lock would let this thread (bridge
        # then emitter) and the frame observer (emitter, then this listener)
        # take the pair in opposite orders.
        self._lock = emitter.lock
        self._pending_stt: tuple[int, int] | None = None   # (final_ns, ttfb_ns)
        self.skipped: dict[str, int] = {}
        emitter.add_listener(self._on_event)

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def _current_turn_start(self) -> int | None:
        return self._emitter.timestamp_of("user_speech.started")

    def on_span(self, kind: str, span: Mapping[str, Any]) -> None:
        """A `span_start` or `span_end` callback carrying a `safe_span` dict."""
        name = span.get("name")
        if name not in ("stt", "llm", "tts") or not self._emitter.active:
            return
        started = kind == "span_start"
        timestamp = span.get("start_time_ns") if started else span.get("end_time_ns")
        if not isinstance(timestamp, int) or isinstance(timestamp, bool):
            return
        with self._lock:
            if started:
                self._on_start(name, timestamp)
            else:
                self._on_end(name, span, timestamp)

    def _on_start(self, name: str, timestamp: int) -> None:
        turn_start = self._current_turn_start()
        if name != "stt" and turn_start is not None and timestamp < turn_start:
            # A span belonging to the previous turn, arriving after this one began.
            self._skip("stale_span")
            return
        self._emitter.emit(f"{name}.started", timestamp, stage=name)

    def _on_end(self, name: str, span: Mapping[str, Any], end_ns: int) -> None:
        ttfb = _ttfb_ns(span.get("attributes") or {})
        if name == "tts":
            return
        if ttfb is None:
            self._skip(f"{name}_ttfb_unmeasured")
            return
        if name == "llm":
            start = span.get("start_time_ns")
            if not isinstance(start, int) or isinstance(start, bool):
                return
            self._emitter.emit("llm.first_token", min(start + ttfb, end_ns),
                               stage="llm")
            return
        # STT: the clock started at the end of speech, so the first transcript
        # is `speech_end + ttfb`, and the final transcript is the span's end.
        self._pending_stt = (end_ns, ttfb)
        self._flush_stt()

    def _flush_stt(self) -> None:
        if self._pending_stt is None:
            return
        speech_end = self._emitter.timestamp_of("user_speech.ended")
        if speech_end is None:
            return   # not known yet; `_on_event` retries when it is
        final_ns, ttfb = self._pending_stt
        self._pending_stt = None
        first_ns = speech_end + ttfb
        if first_ns > final_ns:
            # The measurements disagree. Emit neither rather than reorder them.
            self._skip("stt_inconsistent")
            return
        self._emitter.emit("stt.first", first_ns, stage="stt")
        self._emitter.emit("stt.final", final_ns, stage="stt")

    def _on_event(self, event: Mapping[str, Any]) -> None:
        with self._lock:
            if event["event_name"] == "user_speech.started":
                self._pending_stt = None     # never carry a measurement across turns
            elif event["event_name"] == "user_speech.ended":
                self._flush_stt()
