"""The live-event path: emitter, span bridge, and frame observer.

These pin the properties the observability UI depends on: every event that
reaches the browser is a valid prefix of a contract-v1 trace, no timing is
estimated when a measurement is missing, error text never leaves the process,
and nothing raised here can reach Pipecat.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from voice_agent.pipeline import factory
from voice_agent.telemetry import tracing
from voice_agent.config import Config
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    ErrorFrame,
    FatalErrorFrame,
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.utils.errors import ErrorCategory
from voice_agent.telemetry.runtime_events import SpanEventBridge
from voice_agent.telemetry.contract import RuntimeEventEmitter, validate_trace
from voice_agent.telemetry.tracing import TurnSpanObserver, error_classification

S = 1_000_000_000


def make_emitter():
    sink: list[dict] = []
    emitter = RuntimeEventEmitter(
        configuration_snapshot_id="config_test",
        workload_fixture_id="workload_test",
        session_id="session_test",
        conversation_id="conversation_test",
        callback=sink.append)
    return emitter, sink


def names(sink):
    return [event["event_name"] for event in sink]


# ------------------------------------------------------------------ emitter --

def test_turn_identity_is_stable_and_stages_are_child_spans():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    emitter.emit("endpointing.started", 2 * S, stage="endpointing")
    emitter.emit("endpointing.resolved", 3 * S, stage="endpointing")
    root, started, resolved = (e["identities"] for e in sink)
    assert {i["turn_id"] for i in (root, started, resolved)} == {root["turn_id"]}
    assert started["span_id"] == resolved["span_id"] != root["span_id"]
    assert started["parent_span_id"] == root["span_id"]
    emitter.start_turn(9 * S)
    assert sink[-1]["identities"]["turn_id"] != root["turn_id"]


def test_a_full_turn_is_a_valid_contract_trace():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    for name, ts, stage in [
        ("user_speech.ended", 2 * S, None),
        ("endpointing.started", 2 * S, "endpointing"),
        ("endpointing.resolved", 3 * S, "endpointing"),
        ("tts.started", 4 * S, "tts"),
        ("tts.first_synthesized_sample", 5 * S, "tts"),
        ("output_transport.first_audio", 6 * S, "output_transport"),
    ]:
        assert emitter.emit(name, ts, stage=stage) is not None
    emitter.emit("turn.completed", 7 * S, attributes={"turn.outcome": "completed"})
    validate_trace(sink)
    assert emitter.drops == {}


def test_exactly_one_terminal_and_nothing_after_it():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    assert emitter.emit("turn.completed", 2 * S) is not None
    assert not emitter.active
    assert emitter.emit("turn.failed", 3 * S) is None
    assert emitter.emit("llm.started", 3 * S, stage="llm") is None
    assert names(sink).count("turn.completed") == 1
    assert emitter.drops["no_active_turn"] == 2


def test_terminal_timestamp_never_precedes_what_it_concludes():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    emitter.emit("user_speech.ended", 5 * S)
    terminal = emitter.emit("turn.failed", 2 * S)
    assert terminal["timestamp_ns"] == 5 * S
    validate_trace(sink)


def test_emit_before_a_turn_starts_is_dropped_not_raised():
    emitter, sink = make_emitter()
    assert emitter.emit("llm.started", 1 * S, stage="llm") is None
    assert sink == [] and emitter.drops == {"no_active_turn": 1}


def test_callback_and_listener_failures_never_reach_the_caller():
    def broken(_event):
        raise RuntimeError("provider payload: secret")

    emitter = RuntimeEventEmitter(
        configuration_snapshot_id="config_test", workload_fixture_id="w",
        session_id="s", conversation_id="c", callback=broken)
    emitter.add_listener(broken)
    event = emitter.start_turn(1 * S)
    assert event is not None
    assert emitter.drops == {"callback_failed": 1, "listener_failed": 1}


@pytest.mark.parametrize("name, ts, reason", [
    ("tts.first_synthesized_sample", 5 * S, "missing_prerequisite"),
    ("output_transport.first_audio", 6 * S, "missing_prerequisite"),
    ("stt.final", 6 * S, "missing_prerequisite"),
    ("user_speech.started", 6 * S, "duplicate_boundary"),
])
def test_events_the_contract_would_reject_are_dropped(name, ts, reason):
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    assert emitter.emit(name, ts) is None
    assert emitter.drops == {reason: 1}
    assert names(sink) == ["user_speech.started"]


def test_a_boundary_earlier_than_its_prerequisite_is_dropped():
    emitter, _ = make_emitter()
    emitter.start_turn(1 * S)
    emitter.emit("tts.started", 5 * S, stage="tts")
    assert emitter.emit("tts.first_synthesized_sample", 4 * S, stage="tts") is None
    assert emitter.drops == {"out_of_order": 1}


def test_tool_events_are_not_emittable_without_tool_identity():
    emitter, _ = make_emitter()
    emitter.start_turn(1 * S)
    assert emitter.emit("tool.requested", 2 * S) is None
    assert emitter.drops == {"contract_violation": 1}


def test_a_new_turn_abandons_an_unfinished_one():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    emitter.emit("user_speech.ended", 2 * S)
    emitter.start_turn(3 * S)
    assert emitter.has_event("user_speech.started")
    assert not emitter.has_event("user_speech.ended")
    assert names(sink) == ["user_speech.started", "user_speech.ended",
                           "user_speech.started"]


def test_attributes_are_filtered_to_the_metadata_allowlist():
    emitter, sink = make_emitter()
    emitter.start_turn(1 * S)
    emitter.emit("turn.completed", 2 * S, attributes={
        "turn.outcome": "completed", "transcript": "hello there",
        "error.classification": "/Users/someone/private"})
    assert sink[-1]["attributes"] == {"turn.outcome": "completed"}


# ------------------------------------------------------------------- bridge --

def span(name, start, end=None, **attributes):
    return {"name": name, "start_time_ns": start, "end_time_ns": end,
            "attributes": attributes}


def bridged():
    emitter, sink = make_emitter()
    bridge = SpanEventBridge(emitter)
    emitter.start_turn(1 * S)
    return emitter, bridge, sink


def test_stt_first_is_measured_from_speech_end_not_span_start():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("stt", 1 * S))
    emitter.emit("user_speech.ended", 5 * S)
    bridge.on_span("span_end", span("stt", 1 * S, 6 * S, **{"metrics.ttfb": 0.3}))
    by_name = {e["event_name"]: e["timestamp_ns"] for e in sink}
    assert by_name["stt.started"] == 1 * S
    assert by_name["stt.first"] == 5 * S + 300_000_000
    assert by_name["stt.final"] == 6 * S
    emitter.emit("turn.completed", 7 * S)
    validate_trace(sink)


def test_stt_waits_for_speech_end_and_emits_once_it_is_known():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("stt", 1 * S))
    bridge.on_span("span_end", span("stt", 1 * S, 4 * S, **{"metrics.ttfb": 0.5}))
    assert "stt.final" not in names(sink)
    emitter.emit("user_speech.ended", 3 * S)
    assert names(sink)[-2:] == ["stt.first", "stt.final"]


def test_a_pending_stt_measurement_does_not_leak_into_the_next_turn():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("stt", 1 * S))
    bridge.on_span("span_end", span("stt", 1 * S, 4 * S, **{"metrics.ttfb": 0.5}))
    emitter.start_turn(10 * S)
    emitter.emit("user_speech.ended", 11 * S)
    assert "stt.first" not in names(sink) and "stt.final" not in names(sink)


def test_missing_ttfb_means_no_stt_first_or_final_and_is_counted():
    emitter, bridge, sink = bridged()
    emitter.emit("user_speech.ended", 2 * S)
    bridge.on_span("span_start", span("stt", 1 * S))
    bridge.on_span("span_end", span("stt", 1 * S, 4 * S))
    assert names(sink) == ["user_speech.started", "user_speech.ended", "stt.started"]
    assert bridge.skipped == {"stt_ttfb_unmeasured": 1}


def test_inconsistent_stt_measurements_emit_neither_boundary():
    emitter, bridge, sink = bridged()
    emitter.emit("user_speech.ended", 5 * S)
    bridge.on_span("span_start", span("stt", 1 * S))
    bridge.on_span("span_end", span("stt", 1 * S, 5 * S + 1, **{"metrics.ttfb": 2.0}))
    assert "stt.first" not in names(sink)
    assert bridge.skipped == {"stt_inconsistent": 1}


def test_llm_first_token_is_start_plus_measured_ttfb_and_never_after_the_span():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("llm", 2 * S))
    bridge.on_span("span_end", span("llm", 2 * S, 3 * S, **{"metrics.ttfb": 0.25}))
    assert sink[-1]["event_name"] == "llm.first_token"
    assert sink[-1]["timestamp_ns"] == 2 * S + 250_000_000
    _, bridge2, sink2 = bridged()
    bridge2.on_span("span_start", span("llm", 2 * S))
    bridge2.on_span("span_end", span("llm", 2 * S, 3 * S, **{"metrics.ttfb": 9.0}))
    assert sink2[-1]["timestamp_ns"] == 3 * S


@pytest.mark.parametrize("ttfb", [None, True, False, -0.1, float("nan"),
                                  float("inf"), "0.2"])
def test_an_unusable_ttfb_never_becomes_a_first_token(ttfb):
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("llm", 2 * S))
    attrs = {} if ttfb is None else {"metrics.ttfb": ttfb}
    bridge.on_span("span_end", span("llm", 2 * S, 3 * S, **attrs))
    assert names(sink) == ["user_speech.started", "llm.started"]
    assert bridge.skipped == {"llm_ttfb_unmeasured": 1}


def test_tts_started_is_emitted_and_a_previous_turns_span_is_not():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("tts", 4 * S))
    bridge.on_span("span_start", span("llm", S // 2))    # before this turn began
    assert names(sink) == ["user_speech.started", "tts.started"]
    assert bridge.skipped == {"stale_span": 1}


def test_unrelated_and_incomplete_spans_are_ignored():
    emitter, bridge, sink = bridged()
    bridge.on_span("span_start", span("turn_detection", 2 * S))
    bridge.on_span("span_start", {"name": "llm", "start_time_ns": None})
    bridge.on_span("span_end", span("llm", 2 * S, None))
    assert names(sink) == ["user_speech.started"]


def test_spans_from_another_thread_are_safe():
    emitter, bridge, sink = bridged()
    worker = threading.Thread(
        target=lambda: bridge.on_span("span_start", span("llm", 2 * S)))
    worker.start()
    worker.join()
    assert names(sink) == ["user_speech.started", "llm.started"]


# ---------------------------------------------------------------- observer --

class _FakeSpan:
    def __init__(self, name, start_time):
        self.name, self.start_time, self.end_time = name, start_time, None
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self, end_time):
        self.end_time = end_time


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, start_time):
        span_ = _FakeSpan(name, start_time)
        self.spans.append(span_)
        return span_


class Harness:
    """A TurnSpanObserver wired to an emitter, fed synthetic Pipecat frames."""

    def __init__(self, monkeypatch, **observer_kwargs):
        self.tracer = _FakeTracer()
        monkeypatch.setattr(tracing.trace, "get_tracer", lambda _: self.tracer)
        self.now = 100 * S
        monkeypatch.setattr(tracing.time, "time_ns", lambda: self.now)
        self.emitter, self.sink = make_emitter()
        self.observer = TurnSpanObserver(
            mode="live", event_emitter=self.emitter, **observer_kwargs)
        self.output = object.__new__(BaseOutputTransport)

    def push(self, frame, *, source=None, direction=FrameDirection.DOWNSTREAM):
        data = SimpleNamespace(frame=frame, source=source or object(),
                               destination=object(), direction=direction)
        asyncio.run(self.observer.on_push_frame(data))

    def user_turn(self, *, start_at=98.0, speech_end_at=100.0):
        self.push(VADUserStartedSpeakingFrame(start_secs=0.2, timestamp=start_at))
        self.push(UserStartedSpeakingFrame())
        self.push(VADUserStoppedSpeakingFrame(
            stop_secs=0.4, timestamp=speech_end_at + 0.4))
        self.push(UserStoppedSpeakingFrame())

    def bot_audio(self):
        frame = TTSAudioRawFrame(audio=b"\0\0", sample_rate=24000, num_channels=1)
        self.emitter.emit("tts.started", self.now - 1, stage="tts")   # the span
        self.push(frame)
        self.now += S // 10
        self.push(frame, source=self.output)


def test_speech_start_comes_from_the_vad_frame_not_frame_arrival(monkeypatch):
    h = Harness(monkeypatch)
    h.user_turn(start_at=98.0)
    assert h.sink[0]["event_name"] == "user_speech.started"
    assert h.sink[0]["timestamp_ns"] == int((98.0 - 0.2) * 1e9)


def test_speech_end_and_endpointing_use_pipecats_arithmetic(monkeypatch):
    h = Harness(monkeypatch, endpoint_strategy="vad_timeout")
    h.user_turn(speech_end_at=100.0)
    by_name = {e["event_name"]: e for e in h.sink}
    assert by_name["user_speech.ended"]["timestamp_ns"] == int(100.0 * 1e9)
    assert by_name["endpointing.started"]["timestamp_ns"] == int(100.0 * 1e9)
    assert by_name["endpointing.resolved"]["timestamp_ns"] == h.now
    assert by_name["endpointing.started"]["attributes"] == {"strategy": "vad_timeout"}


@pytest.mark.parametrize("strategy", ["smart_turn", "vad_timeout"])
def test_endpointing_strategy_metadata_follows_the_configured_runtime(
        monkeypatch, strategy):
    monkeypatch.setattr(factory, "endpointing_strategy", lambda config=None: strategy)
    h = Harness(monkeypatch)
    h.user_turn()
    started = next(e for e in h.sink if e["event_name"] == "endpointing.started")
    assert started["attributes"] == {"strategy": strategy}
    detection = next(s for s in h.tracer.spans if s.name == "turn_detection")
    assert detection.attributes["strategy"] == strategy


def test_strategy_is_derived_from_config_rather_than_hard_coded():
    assert factory.endpointing_strategy(Config(use_smart_turn=True)) == "smart_turn"
    assert factory.endpointing_strategy(Config(use_smart_turn=False)) == "vad_timeout"


def test_an_explicit_strategy_overrides_configuration(monkeypatch):
    h = Harness(monkeypatch, endpoint_strategy="smart_turn")
    h.user_turn()
    started = next(e for e in h.sink if e["event_name"] == "endpointing.started")
    assert started["attributes"] == {"strategy": "smart_turn"}


def test_a_complete_observed_turn_is_a_valid_trace_that_ends_completed(monkeypatch):
    h = Harness(monkeypatch)
    h.user_turn()
    h.bot_audio()
    h.push(BotStoppedSpeakingFrame())
    assert names(h.sink) == [
        "user_speech.started", "user_speech.ended", "endpointing.started",
        "endpointing.resolved", "tts.started", "tts.first_synthesized_sample",
        "output_transport.first_audio", "turn.completed"]
    validate_trace(h.sink)
    assert h.emitter.drops == {}


def test_bot_stop_without_accepted_audio_leaves_the_turn_without_an_outcome(
        monkeypatch):
    h = Harness(monkeypatch)
    h.user_turn()
    h.push(BotStoppedSpeakingFrame())
    assert "turn.completed" not in names(h.sink)
    assert h.emitter.active


def test_synthesis_without_tts_started_cannot_produce_first_audio(monkeypatch):
    """The contract needs `tts.started`; nothing is invented to satisfy it."""
    h = Harness(monkeypatch)
    h.user_turn()
    frame = TTSAudioRawFrame(audio=b"\0\0", sample_rate=24000, num_channels=1)
    h.push(frame)
    h.push(frame, source=h.output)
    assert "output_transport.first_audio" not in names(h.sink)
    assert set(h.emitter.drops) == {"missing_prerequisite"}


def test_error_before_audio_fails_the_turn_with_a_fixed_code_and_no_text(
        monkeypatch):
    h = Harness(monkeypatch)
    h.user_turn()
    secret = "sk-live-SECRET connect to db.internal.example.com /Users/me/key"
    frame = ErrorFrame(error=secret, category=ErrorCategory.CONNECTIVITY,
                       exception=RuntimeError(secret))
    h.push(frame, direction=FrameDirection.UPSTREAM)
    h.push(frame, direction=FrameDirection.UPSTREAM)        # every hop repeats it
    failed = [e for e in h.sink if e["event_name"] == "turn.failed"]
    assert len(failed) == 1
    assert failed[0]["attributes"] == {
        "turn.outcome": "failed", "error.classification": "provider_connectivity"}
    assert "SECRET" not in json.dumps(h.sink) and "example.com" not in json.dumps(h.sink)
    validate_trace(h.sink)


def test_recoverable_error_after_first_audio_does_not_fail_the_turn(monkeypatch):
    h = Harness(monkeypatch)
    h.user_turn()
    h.bot_audio()
    h.push(ErrorFrame(error="hiccup"), direction=FrameDirection.UPSTREAM)
    assert "turn.failed" not in names(h.sink)
    h.push(FatalErrorFrame(error="gone"), direction=FrameDirection.UPSTREAM)
    assert names(h.sink)[-1] == "turn.failed"


def test_error_with_no_active_turn_is_ignored(monkeypatch):
    h = Harness(monkeypatch)
    h.push(ErrorFrame(error="x"), direction=FrameDirection.UPSTREAM)
    assert h.sink == []


@pytest.mark.parametrize("category, expected", [
    (ErrorCategory.AUTHENTICATION, "provider_authentication"),
    (ErrorCategory.RATE_LIMIT, "provider_rate_limit"),
    (ErrorCategory.APPLICATION, "application_error"),
    (ErrorCategory.UNKNOWN, "pipeline_error"),
    (None, "pipeline_error"),
])
def test_error_classification_is_a_fixed_code(category, expected):
    assert error_classification(ErrorFrame(error="raw", category=category)) == expected


def test_a_failing_emitter_cannot_break_the_observer(monkeypatch):
    h = Harness(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(h.emitter, "emit", boom)
    monkeypatch.setattr(h.emitter, "start_turn", boom)
    h.user_turn()      # must not raise
