"""Contract 1.1.0: the retrieval stage, and 1.0.0 traces staying valid."""

from __future__ import annotations

from pathlib import Path

import pytest

from voice_agent import paths
from voice_agent.telemetry.contract import (
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    ContractError,
    RuntimeEventEmitter,
    filter_metadata,
    load_jsonl,
    validate_event,
    validate_trace,
)

FIXTURES = paths.TELEMETRY_FIXTURES_DIR
S = 1_000_000_000


def event(name, ts, *, version=SCHEMA_VERSION, attributes=None, n=[0]):
    n[0] += 1
    return {
        "schema_version": version, "event_id": f"evt_{n[0]}", "event_name": name,
        "timestamp_ns": ts,
        "identities": {
            "session_id": "s", "conversation_id": "c", "turn_id": "t", "trace_id": "tr",
            "span_id": "sp", "parent_span_id": None, "logical_tool_call_id": None,
            "tool_attempt_id": None, "configuration_snapshot_id": "cfg", "workload_fixture_id": "w"},
        "attributes": attributes or {},
    }


def test_version_is_1_1_0_and_1_0_0_is_still_understood():
    assert SCHEMA_VERSION == "1.1.0" and LEGACY_SCHEMA_VERSION == "1.0.0"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.jsonl")))
def test_every_fixture_still_validates(path):
    validate_trace(load_jsonl(path))


@pytest.mark.parametrize("name", ["normal-completed", "slow-blocking-tool", "interrupted", "failed-tool"])
def test_the_original_four_fixtures_are_unchanged_1_0_0_traces(name):
    events = load_jsonl(FIXTURES / f"{name}.jsonl")
    assert {e["schema_version"] for e in events} == {"1.0.0"}
    assert not [e for e in events if e["event_name"].startswith("retrieval.")]


def test_retrieval_events_are_valid_at_1_1_0_and_carry_metadata_only():
    validate_event(event("retrieval.started", S))
    validate_event(event("retrieval.completed", 2 * S, attributes={
        "retrieval.match_count": 3, "retrieval.method": "bm25",
        "retrieval.top_source": "library.opening-hours"}))


@pytest.mark.parametrize("name", ["retrieval.started", "retrieval.completed"])
def test_a_1_0_0_event_cannot_be_a_retrieval_event(name):
    with pytest.raises(ContractError, match="requires schema_version 1.1.0"):
        validate_event(event(name, S, version="1.0.0"))


def test_unknown_future_versions_still_fail_closed():
    with pytest.raises(ContractError, match="unsupported schema_version"):
        validate_event(event("user_speech.started", S, version="1.2.0"))


def test_retrieval_completed_cannot_precede_started_or_appear_alone():
    with pytest.raises(ContractError):
        validate_trace([event("user_speech.started", S), event("retrieval.completed", 2 * S),
                        event("turn.completed", 3 * S)])
    with pytest.raises(ContractError):
        validate_trace([event("user_speech.started", S), event("retrieval.started", 3 * S),
                        event("retrieval.completed", 2 * S), event("turn.completed", 4 * S)])
    validate_trace([event("user_speech.started", S), event("retrieval.started", 2 * S),
                    event("retrieval.completed", 3 * S), event("turn.completed", 4 * S)])


@pytest.mark.parametrize("attributes", [
    {"retrieval.match_count": "3"}, {"retrieval.match_count": True},
    {"retrieval.method": "BM25 v2"}, {"retrieval.top_source": "/Users/me/notes.md"},
    {"retrieval.top_source": "has spaces"},
])
def test_unsafe_retrieval_attributes_are_not_accepted(attributes):
    assert filter_metadata(attributes) == {}
    with pytest.raises(ContractError):
        validate_event(event("retrieval.completed", S, attributes=attributes))


def test_retrieval_attribute_names_cannot_smuggle_content():
    for key in ("retrieval.query", "retrieval.text", "retrieval.passages", "retrieval.result"):
        assert filter_metadata({key: "what time do you close"}) == {}


def test_the_emitter_publishes_retrieval_as_a_valid_trace():
    sink = []
    emitter = RuntimeEventEmitter(configuration_snapshot_id="cfg", workload_fixture_id="w",
                                  session_id="s", conversation_id="c", callback=sink.append)
    emitter.start_turn(S)
    assert emitter.emit("retrieval.started", 2 * S, stage="retrieval")
    assert emitter.emit("retrieval.completed", 3 * S, stage="retrieval", attributes={
        "retrieval.match_count": 2, "retrieval.method": "bm25"})
    assert emitter.emit("turn.completed", 4 * S)
    validate_trace(sink)
    started, completed = sink[1], sink[2]
    assert started["identities"]["span_id"] == completed["identities"]["span_id"]
    assert started["schema_version"] == SCHEMA_VERSION


def test_a_second_retrieval_in_one_turn_is_dropped_not_merged():
    sink = []
    emitter = RuntimeEventEmitter(configuration_snapshot_id="cfg", workload_fixture_id="w",
                                  session_id="s", conversation_id="c", callback=sink.append)
    emitter.start_turn(S)
    emitter.emit("retrieval.started", 2 * S, stage="retrieval")
    assert emitter.emit("retrieval.started", 3 * S, stage="retrieval") is None
    assert emitter.drops == {"duplicate_boundary": 1}
