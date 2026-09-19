"""Focused tests for the repository-owned Phase 1 telemetry contract."""

from __future__ import annotations

import json
import asyncio
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import Config
from pipecat.frames.frames import (
    TTSAudioRawFrame,
    UserStartedSpeakingFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_output import BaseOutputTransport
import tracing_setup
from tracing_setup import JsonlSpanExporter, LiveSpanProcessor, TurnSpanObserver
from telemetry import (
    ContractError,
    EVENT_NAMES,
    SCHEMA_VERSION,
    TelemetryIdentity,
    ToolAttemptIdentity,
    configuration_snapshot,
    filter_metadata,
    load_jsonl,
    redact_text,
    safe_span,
    validate_event,
    validate_trace,
)

FIXTURES = Path("telemetry_fixtures")
SCHEMA = Path("telemetry_contract/v1.schema.json")


def _event(name="turn.completed"):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "event_1",
        "event_name": name,
        "timestamp_ns": 1,
        "identities": {
            "session_id": "session_1",
            "conversation_id": "conversation_1",
            "turn_id": "turn_1",
            "trace_id": "trace_1",
            "span_id": "span_1",
            "parent_span_id": None,
            "logical_tool_call_id": None,
            "tool_attempt_id": None,
            "configuration_snapshot_id": "config_1",
            "workload_fixture_id": "workload_1",
        },
        "attributes": {"turn.outcome": "completed"},
    }


def test_repository_schema_matches_runtime_contract():
    schema = json.loads(SCHEMA.read_text())
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert set(schema["properties"]["event_name"]["enum"]) == EVENT_NAMES
    assert schema["additionalProperties"] is False
    validate_event(_event())


def test_unknown_schema_versions_fail_closed():
    event = _event()
    event["schema_version"] = "2.0.0"
    with pytest.raises(ContractError, match="unsupported schema_version"):
        validate_event(event)


def test_identity_is_stable_across_child_spans():
    root = TelemetryIdentity.new("config_1", "workload_1")
    child = root.child_span()
    assert child.span_id != root.span_id
    assert child.parent_span_id == root.span_id
    for field in ("session_id", "conversation_id", "turn_id", "trace_id",
                  "configuration_snapshot_id", "workload_fixture_id"):
        assert getattr(child, field) == getattr(root, field)


def test_parent_span_must_exist_in_trace():
    events = load_jsonl(FIXTURES / "normal-completed.jsonl")
    events[3]["identities"]["parent_span_id"] = "span_missing"
    with pytest.raises(ContractError, match="missing parent span"):
        validate_trace(events)


def test_parent_span_cycles_are_rejected():
    events = load_jsonl(FIXTURES / "normal-completed.jsonl")
    for event in events:
        if event["identities"]["span_id"] == "span_turn_normal":
            event["identities"]["parent_span_id"] = "span_endpoint_normal"
    with pytest.raises(ContractError, match="span parent cycle"):
        validate_trace(events)


def test_configuration_snapshot_is_immutable_and_excludes_raw_config():
    snapshot = configuration_snapshot(
        Config(system_prompt="sensitive prompt", audio_device=42),
        prompt_revision_id="prompt_revision_7", source_revision="abc123")
    with pytest.raises(FrozenInstanceError):
        snapshot.seed = 99
    serialized = json.dumps(snapshot.as_dict(), sort_keys=True)
    assert "sensitive prompt" not in serialized
    assert "audio_device" not in serialized
    assert "prompt_revision_7" in serialized
    mutable_copy = snapshot.as_dict()
    mutable_copy["endpointing_settings"]["vad_stop_secs"] = 99
    assert dict(snapshot.endpointing_settings)["vad_stop_secs"] == 0.5
    assert snapshot.snapshot_id == configuration_snapshot(
        Config(system_prompt="different content", audio_device=7),
        prompt_revision_id="prompt_revision_7", source_revision="abc123",
    ).snapshot_id


@pytest.mark.parametrize("change", [
    {"llm_temperature": 0.7}, {"llm_max_tokens": 99},
    {"vad_min_volume": 0.6}, {"input_sample_rate": 8000},
    {"output_sample_rate": 16000}, {"filter_incomplete_user_turns": True},
    {"stt_model": "mlx-community/whisper-tiny"},
    {"tts_voice": "af_bella"},
])
def test_behavior_changes_snapshot_identity(change):
    baseline = configuration_snapshot(
        Config(), prompt_revision_id="prompt-v1", source_revision="abc123")
    changed = configuration_snapshot(
        Config(**change), prompt_revision_id="prompt-v1", source_revision="abc123")
    assert changed.snapshot_id != baseline.snapshot_id


def test_snapshot_selects_models_and_voices_for_actual_engines():
    default = configuration_snapshot(Config(), prompt_revision_id="prompt-v1")
    faster = configuration_snapshot(
        Config(stt_engine="faster-whisper", stt_model_faster_whisper="small"),
        prompt_revision_id="prompt-v1")
    piper = configuration_snapshot(
        Config(tts_engine="piper", tts_voice_piper="en_US-lessac-medium"),
        prompt_revision_id="prompt-v1")
    assert dict(default.model_ids)["stt"] == Config().stt_model
    assert dict(faster.model_ids)["stt"] == "small"
    assert dict(piper.model_ids)["tts"] == "en_US-lessac-medium"
    assert len({default.snapshot_id, faster.snapshot_id, piper.snapshot_id}) == 3


def test_privacy_filter_drops_raw_content_and_unknown_provider_fields():
    attributes = {
        "transcript": "private words",
        "gen_ai.system_instructions": "private prompt",
        "input": "raw request",
        "output": "raw response",
        "text": "tts text",
        "tool.arguments": "private args",
        "tool.results": "private result",
        "exception.message": "private error",
        "provider.payload": "opaque payload",
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "fixture-model",
        "metrics.ttfb": 0.25,
    }
    assert filter_metadata(attributes) == {
        "gen_ai.provider.name": "ollama",
        "gen_ai.request.model": "fixture-model",
        "metrics.ttfb": 0.25,
    }


def test_redaction_removes_credentials_and_absolute_paths():
    redacted = redact_text(
        "api_key=secret-value failed in /Users/example/project/file.py")
    assert "secret-value" not in redacted
    assert "/Users/example" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_progress_and_error_values_must_be_controlled_codes():
    event = _event("tool.progress")
    event["identities"]["logical_tool_call_id"] = "tool_call_1"
    event["identities"]["tool_attempt_id"] = "tool_attempt_1"
    event["attributes"] = {
        "tool.name": "lookup", "tool.state": "progress",
        "tool.attempt_number": 1, "tool.progress_code": "raw user content",
    }
    with pytest.raises(ContractError, match="controlled code"):
        validate_event(event)


def test_span_serialization_always_applies_privacy_filter():
    span = {
        "name": "llm", "trace_id": "trace", "span_id": "span",
        "parent_span_id": None, "start_time_ns": 1, "end_time_ns": 2,
        "duration_ms": 0.000001,
        "attributes": {"output": "private", "metrics.ttfb": 0.1},
    }
    assert safe_span(span)["attributes"] == {"metrics.ttfb": 0.1}


def test_timing_boundaries_require_start_and_order():
    events = load_jsonl(FIXTURES / "normal-completed.jsonl")
    without_start = [e for e in events if e["event_name"] != "tts.started"]
    with pytest.raises(ContractError, match="requires tts.started"):
        validate_trace(without_start)
    reversed_events = load_jsonl(FIXTURES / "normal-completed.jsonl")
    first_audio = next(e for e in reversed_events
                       if e["event_name"] == "output_transport.first_audio")
    first_audio["timestamp_ns"] = 2_000_000_000
    with pytest.raises(ContractError, match="precedes"):
        validate_trace(reversed_events)


def test_event_record_order_does_not_change_timestamp_semantics():
    events = load_jsonl(FIXTURES / "normal-completed.jsonl")
    events[2], events[3] = events[3], events[2]
    validate_trace(events)


def test_tool_retry_attempts_share_logical_id_and_have_distinct_attempt_ids():
    events = load_jsonl(FIXTURES / "failed-tool.jsonl")
    validate_trace(events)
    tool_events = [e for e in events if e["event_name"].startswith("tool.")]
    assert {e["identities"]["logical_tool_call_id"] for e in tool_events} == {
        "tool_call_failed"}
    assert {e["identities"]["tool_attempt_id"] for e in tool_events} == {
        "tool_attempt_failed_1", "tool_attempt_failed_2"}
    assert {e["attributes"]["tool.attempt_number"] for e in tool_events} == {1, 2}


def test_tool_identity_retry_helper_preserves_logical_call():
    first = ToolAttemptIdentity.new()
    second = first.retry()
    assert second.logical_tool_call_id == first.logical_tool_call_id
    assert second.tool_attempt_id != first.tool_attempt_id
    assert second.attempt_number == 2


def test_arbitrary_telemetry_names_are_not_allowlisted():
    assert filter_metadata({"telemetry.custom": "could contain raw content"}) == {}


@pytest.mark.parametrize("key", [
    "tool.name", "tool.progress_code", "error.classification",
    "gen_ai.provider.name", "gen_ai.request.model", "settings.voice",
    "telemetry.session_id",
])
@pytest.mark.parametrize("value", [
    "user said hello world", "person@example.com", "https://example.com/x",
    "/Users/person/file", r"C:\\Users\\person\\file", "~/private/file",
    "token=secret", "Bearer abc.def", "ValueError: raw exception",
    "host.example.com", {"nested": "arbitrary text"},
])
def test_uncontrolled_values_are_dropped_at_persistence_boundary(key, value):
    assert filter_metadata({key: value}) == {}


_MODEL_ATTRIBUTE_KEYS = (
    "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.operation.name",
    "gen_ai.output.type", "param.model", "voice_id", "settings.voice",
    "settings.model", "settings.engine",
)


@pytest.mark.parametrize("key", _MODEL_ATTRIBUTE_KEYS)
@pytest.mark.parametrize("value", [
    "Users/name/models/x", "models/a/b", "a/b/c", "/Users/name/model",
    "~/models/private", r"C:\Users\name\model", "https://example.com/model",
    "token=secret", "Bearer secret", "x" * 97,
    "sk-abcdef1234567890abcdef",
    "ghp_abcdefghijklmnopqrstuvwxyz123456",
    "gho_abcdefghijklmnopqrstuvwxyz123456",
    "ghu_abcdefghijklmnopqrstuvwxyz123456",
    "ghs_abcdefghijklmnopqrstuvwxyz123456",
    "github_pat_abcdefghijklmnopqrstuvwxyz123456",
    "xoxb-123456789012-abcdefghijklmnopqrstuv",
    "AKIAIOSFODNN7EXAMPLE",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
    "Kob1a6pCeXIPmAlWOXwcVoS5mYU++HK7SFtG01L",
    "Kob1a6pCeXI/PmAlWOXwcVoS5mYU++HK7SFtG01L",
    "deadbeefdeadbeefdeadbeefdeadbeef",
    "AbCdEfGhIjKlMnOpQrStUvWxYz0123_-",
    "OpaqueSecretValue1234567890ABCDEFG",
    "openai/deadbeefdeadbeefdeadbeefdeadbeef",
    "acme/OpaqueSecretValue1234567890ABCDEFG",
    "acme/sk-abcdef1234567890",
    "a/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature",
    "openai/AbCdEfGhIjKlMnOpQrStUvWxYz0123_-",
])
def test_model_provider_voice_values_reject_unsafe_or_token_shaped_values(
        key, value):
    assert filter_metadata({key: value}) == {}


@pytest.mark.parametrize("key", _MODEL_ATTRIBUTE_KEYS)
@pytest.mark.parametrize("value", [
    "openai/gpt", "faster-whisper", "piper",
    "mlx-community/whisper-large-v3-turbo-q4", "llama3.2:3b",
    "af_heart", "en_US-ryan-high", "Systran/faster-whisper-large-v3",
    "hexgrad/Kokoro-82M",
])
def test_model_provider_voice_values_accept_supported_identifiers(key, value):
    assert filter_metadata({key: value}) == {key: value}


@pytest.mark.parametrize("mutation", [
    lambda e: e.update({"payload": "extra"}),
    lambda e: e["identities"].update({"extra": "id"}),
    lambda e: e.pop("attributes"),
    lambda e: e.update({"attributes": None}),
])
def test_validator_rejects_malformed_objects_with_contract_error(mutation):
    event = _event()
    mutation(event)
    with pytest.raises(ContractError):
        validate_event(event)


def test_validator_rejects_bool_attempt_number():
    event = _event("tool.completed")
    event["identities"]["logical_tool_call_id"] = "tool_call_1"
    event["identities"]["tool_attempt_id"] = "tool_attempt_1"
    event["attributes"] = {"tool.attempt_number": True}
    with pytest.raises(ContractError):
        validate_event(event)


def test_validator_rejects_bool_timestamp():
    event = _event()
    event["timestamp_ns"] = True
    with pytest.raises(ContractError):
        validate_event(event)


@pytest.mark.parametrize("bad_id", [
    "user said hello", "/tmp/id", "white space", "ignore previous prompt",
])
def test_validator_rejects_unsafe_ids(bad_id):
    event = _event()
    event["event_id"] = bad_id
    with pytest.raises(ContractError):
        validate_event(event)


def test_validator_accepts_safe_ids():
    validate_event(_event())


@pytest.mark.parametrize("field", [
    "session_id", "conversation_id", "turn_id", "trace_id", "span_id",
    "configuration_snapshot_id", "workload_fixture_id",
])
def test_validator_applies_safe_pattern_to_identity_fields(field):
    event = _event()
    event["identities"][field] = "unsafe identifier"
    with pytest.raises(ContractError):
        validate_event(event)


def test_non_string_controlled_code_is_contract_error():
    event = _event()
    event["attributes"] = {"error.classification": 500}
    with pytest.raises(ContractError):
        validate_event(event)


@pytest.mark.parametrize("field", ["prompt_revision_id", "source_revision"])
def test_snapshot_revisions_require_safe_identifiers(field):
    kwargs = {"prompt_revision_id": "prompt-v1", "source_revision": "abc123"}
    kwargs[field] = "unsafe revision text"
    with pytest.raises(ValueError):
        configuration_snapshot(Config(), **kwargs)


class _FakeSpan:
    def __init__(self, name, start_time):
        self.name = name
        self.start_time = start_time
        self.end_time = None
        self.attributes = {}

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def end(self, end_time):
        self.end_time = end_time


class _FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, start_time):
        span = _FakeSpan(name, start_time)
        self.spans.append(span)
        return span


def test_observer_separates_synthesis_from_transport_acceptance(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(tracing_setup.trace, "get_tracer", lambda _: tracer)
    ticks = iter([2_000_000_000, 2_100_000_000, 2_200_000_000])
    monkeypatch.setattr(tracing_setup.time, "time_ns", lambda: next(ticks))
    observer = TurnSpanObserver(mode="file", fixture="synthetic")
    observer._speech_end = 1.0
    observer._window_open = True
    frame = TTSAudioRawFrame(audio=b"\0\0", sample_rate=24000, num_channels=1)
    output = object.__new__(BaseOutputTransport)
    tts_push = SimpleNamespace(
        frame=frame, source=object(), destination=output,
        direction=FrameDirection.DOWNSTREAM)
    accepted_push = SimpleNamespace(
        frame=frame, source=output, destination=object(),
        direction=FrameDirection.DOWNSTREAM)

    asyncio.run(observer.on_push_frame(accepted_push))
    assert tracer.spans == []
    asyncio.run(observer.on_push_frame(tts_push))
    assert [span.name for span in tracer.spans] == ["tts.first_synthesized_sample"]
    resumed = SimpleNamespace(
        frame=VADUserStartedSpeakingFrame(), source=object(), destination=object(),
        direction=FrameDirection.DOWNSTREAM)
    asyncio.run(observer.on_push_frame(resumed))
    asyncio.run(observer.on_push_frame(tts_push))
    observer._speech_end = 1.0
    observer._window_open = True
    asyncio.run(observer.on_push_frame(accepted_push))
    asyncio.run(observer.on_push_frame(accepted_push))

    names = [span.name for span in tracer.spans]
    assert names.count("tts.first_synthesized_sample") == 1
    assert names.count("output_transport.first_audio") == 1
    assert names.count("e2e.speech_end_to_first_audio") == 1
    synth = next(s for s in tracer.spans if s.name == "tts.first_synthesized_sample")
    accepted = next(s for s in tracer.spans if s.name == "output_transport.first_audio")
    window = next(s for s in tracer.spans if s.name == "e2e.speech_end_to_first_audio")
    assert accepted.start_time >= synth.start_time
    assert accepted.attributes["measured_as"] == "output_transport_accepted"
    assert window.attributes["measured_as"] == "output_transport_accepted"


def test_new_logical_turn_resets_unwritten_first_boundary_state(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(tracing_setup.trace, "get_tracer", lambda _: tracer)
    ticks = iter([2_000_000_000, 3_000_000_000, 4_000_000_000])
    monkeypatch.setattr(tracing_setup.time, "time_ns", lambda: next(ticks))
    observer = TurnSpanObserver(mode="file", fixture="synthetic")
    output = object.__new__(BaseOutputTransport)
    frame = TTSAudioRawFrame(audio=b"\0\0", sample_rate=24000, num_channels=1)

    def pushed(current_frame, source=object(), destination=object()):
        return SimpleNamespace(
            frame=current_frame, source=source, destination=destination,
            direction=FrameDirection.DOWNSTREAM)

    asyncio.run(observer.on_push_frame(pushed(UserStartedSpeakingFrame())))
    observer._speech_end = 1.0
    observer._window_open = True
    asyncio.run(observer.on_push_frame(pushed(frame, destination=output)))
    first_synth = observer._first_synthesized_sample_ns

    asyncio.run(observer.on_push_frame(pushed(UserStartedSpeakingFrame())))
    assert observer._previous_logical_turn_outcome == "interrupted_or_unwritten"
    assert observer._first_synthesized_sample_ns is None
    assert observer._window_open is False
    observer._speech_end = 2.5
    observer._window_open = True
    asyncio.run(observer.on_push_frame(pushed(frame, destination=output)))
    second_synth = observer._first_synthesized_sample_ns
    asyncio.run(observer.on_push_frame(pushed(frame, source=output)))
    asyncio.run(observer.on_push_frame(pushed(frame, source=output)))

    names = [span.name for span in tracer.spans]
    assert names.count("tts.first_synthesized_sample") == 2
    assert names.count("output_transport.first_audio") == 1
    assert names.count("e2e.speech_end_to_first_audio") == 1
    assert second_synth > first_synth
    output_span = next(
        span for span in tracer.spans if span.name == "output_transport.first_audio")
    window = next(
        span for span in tracer.spans if span.name == "e2e.speech_end_to_first_audio")
    assert output_span.start_time >= second_synth
    assert window.end_time == output_span.start_time
    assert observer._logical_turn_state == "completed"


class _ReadableSpan:
    name = "llm"
    start_time = 1
    end_time = 2
    parent = None
    attributes = {"output": "secret", "metrics.ttfb": 0.25}

    def get_span_context(self):
        return SimpleNamespace(trace_id=1, span_id=2)


def test_file_and_live_paths_emit_equivalent_safe_metadata(tmp_path):
    span = _ReadableSpan()
    exporter = JsonlSpanExporter(tmp_path / "trace.jsonl")
    exporter.export([span])
    callbacks = []
    LiveSpanProcessor(lambda kind, value: callbacks.append(value)).on_end(span)
    persisted = load_jsonl(tmp_path / "trace.jsonl")[0]
    assert callbacks == [persisted]
    assert persisted["attributes"] == {"metrics.ttfb": 0.25}


def test_live_workloads_are_unique_and_noncomparable():
    from agent import workload_identity
    first = workload_identity("live", None)
    second = workload_identity("live", None)
    assert first[0] != second[0]
    assert first[1] is second[1] is False
    assert workload_identity("file", "fixture-1") == ("workload_fixture-1", True)


def test_local_control_rejects_bad_host_and_origin():
    from live import local_control_only

    async def handler(_request):
        return "ok"

    bad_host = SimpleNamespace(
        path="/start", headers={"Host": "example.com"})
    bad_origin = SimpleNamespace(
        path="/stop", headers={"Host": "127.0.0.1:8080",
                               "Origin": "http://example.com"})
    good = SimpleNamespace(
        path="/start", headers={"Host": "127.0.0.1:8080",
                                "Origin": "http://127.0.0.1:8080"})
    assert asyncio.run(local_control_only(bad_host, handler)).status == 403
    assert asyncio.run(local_control_only(bad_origin, handler)).status == 403
    assert asyncio.run(local_control_only(good, handler)) == "ok"


def test_browser_stack_uses_model_value_filter(monkeypatch):
    import live
    monkeypatch.setattr(live.factory, "component_identity", lambda: {
        "stt_engine": "mlx", "stt_model": "Users/name/models/private",
        "llm_provider": "ollama", "llm_model": "llama3.2:3b",
        "tts_engine": "kokoro", "tts_voice": "af_heart",
    })
    assert live.browser_stack_description() == "configuration_invalid"


@pytest.mark.parametrize("error, expected", [
    (ValueError("raw secret"), "validation_error"),
    (OSError("raw host failure"), "transport_error"),
    (RuntimeError("raw provider response"), "pipeline_error"),
    (asyncio.CancelledError(), "cancelled"),
])
def test_browser_errors_use_fixed_controlled_classifications(error, expected):
    from live import browser_error_classification
    assert browser_error_classification(error) == expected


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.jsonl")))
def test_synthetic_fixture_is_valid_and_metadata_only(path):
    events = load_jsonl(path)
    validate_trace(events)
    serialized = path.read_text().lower()
    forbidden = (
        '"transcript"', '"prompt"', '"text"', '"input"', '"output"',
        '"argument"', '"result"', '"payload"', '"exception"',
        '"traceback"', '"hostname"', '"username"', '"api_key"',
        '"device_id"', '"device_name"', '"raw_audio"',
    )
    assert not any(term in serialized for term in forbidden)


@pytest.mark.parametrize("path", [
    Path("artifacts/reference-traces.jsonl"),
    Path("artifacts/scheduling-comparison.jsonl"),
    Path("artifacts/turn-detection/false-endpoint-traces.jsonl"),
])
def test_committed_legacy_trace_attributes_are_metadata_only(path):
    for span in load_jsonl(path):
        assert filter_metadata(span.get("attributes")) == span.get("attributes")


def test_turn_outcomes_are_not_mixed():
    expected = {
        "normal-completed.jsonl": "turn.completed",
        "slow-blocking-tool.jsonl": "turn.completed",
        "interrupted.jsonl": "turn.interrupted",
        "failed-tool.jsonl": "turn.failed",
    }
    for name, terminal in expected.items():
        events = load_jsonl(FIXTURES / name)
        assert [e["event_name"] for e in events if e["event_name"].startswith("turn.")] == [terminal]
