"""Versioned, metadata-only telemetry primitives for voice-agent turns.

The public contract is ``telemetry_contract/v1.schema.json``.  This module
implements the same rules without adding a schema-validation dependency and is
the single privacy boundary used before telemetry is persisted or displayed.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import uuid
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = "1.0.0"
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
CONTROLLED_CODE_PATTERN = re.compile(r"[a-z0-9_.-]{1,64}\Z")
MODEL_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.:+-]*(?:/[A-Za-z0-9][A-Za-z0-9_.:+-]*)?\Z")
MODEL_ID_MAX_LENGTH = 96
_MODEL_SECRET_PREFIXES = (
    "sk-", "ghp_", "gho_", "ghu_", "ghs_", "github_pat_", "xox",
    "akia", "eyj",
)
_MODEL_JWT_PATTERN = re.compile(
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\Z")
_MODEL_HEX_TOKEN_PATTERN = re.compile(r"[A-Fa-f0-9]{32,}\Z")
_MODEL_BASE64_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_+/-]{32,}={0,2}\Z")

EVENT_NAMES = frozenset({
    "user_speech.started", "user_speech.ended",
    "endpointing.started", "endpointing.resolved",
    "stt.started", "stt.first", "stt.final",
    "llm.started", "llm.first_token",
    "tool.requested", "tool.running", "tool.progress",
    "tool.completed", "tool.error", "tool.cancelled",
    "tts.started", "tts.first_synthesized_sample",
    "output_transport.first_audio",
    "turn.completed", "turn.interrupted", "turn.failed",
})

TURN_TERMINALS = frozenset({
    "turn.completed", "turn.interrupted", "turn.failed",
})
TOOL_EVENTS = frozenset(name for name in EVENT_NAMES if name.startswith("tool."))

_ID_FIELDS = (
    "session_id", "conversation_id", "turn_id", "trace_id", "span_id",
    "parent_span_id", "logical_tool_call_id", "tool_attempt_id",
    "configuration_snapshot_id", "workload_fixture_id",
)
_REQUIRED_IDS = (
    "session_id", "conversation_id", "turn_id", "trace_id", "span_id",
    "configuration_snapshot_id", "workload_fixture_id",
)


def _new_id(kind: str) -> str:
    return f"{kind}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class TelemetryIdentity:
    """Stable identity shared by every event for one turn."""

    session_id: str
    conversation_id: str
    turn_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    logical_tool_call_id: str | None
    tool_attempt_id: str | None
    configuration_snapshot_id: str
    workload_fixture_id: str

    @classmethod
    def new(cls, configuration_snapshot_id: str, workload_fixture_id: str,
            *, session_id: str | None = None,
            conversation_id: str | None = None,
            turn_id: str | None = None,
            trace_id: str | None = None,
            span_id: str | None = None,
            parent_span_id: str | None = None,
            logical_tool_call_id: str | None = None,
            tool_attempt_id: str | None = None) -> TelemetryIdentity:
        return cls(
            session_id or _new_id("session"),
            conversation_id or _new_id("conversation"),
            turn_id or _new_id("turn"),
            trace_id or _new_id("trace"),
            span_id or _new_id("span"),
            parent_span_id,
            logical_tool_call_id,
            tool_attempt_id,
            configuration_snapshot_id,
            workload_fixture_id,
        )

    def child_span(self, *, logical_tool_call_id: str | None = None,
                   tool_attempt_id: str | None = None) -> TelemetryIdentity:
        return TelemetryIdentity(
            self.session_id, self.conversation_id, self.turn_id, self.trace_id,
            _new_id("span"), self.span_id,
            logical_tool_call_id, tool_attempt_id,
            self.configuration_snapshot_id, self.workload_fixture_id,
        )

    def as_dict(self) -> dict[str, str | None]:
        return {name: getattr(self, name) for name in _ID_FIELDS}


@dataclass(frozen=True)
class ToolAttemptIdentity:
    """One retryable attempt belonging to a stable logical tool call."""

    logical_tool_call_id: str
    tool_attempt_id: str
    attempt_number: int

    @classmethod
    def new(cls, attempt_number: int = 1,
            logical_tool_call_id: str | None = None) -> ToolAttemptIdentity:
        if attempt_number < 1:
            raise ValueError("attempt_number must be >= 1")
        return cls(logical_tool_call_id or _new_id("tool_call"),
                   _new_id("tool_attempt"), attempt_number)

    def retry(self) -> ToolAttemptIdentity:
        return ToolAttemptIdentity(
            self.logical_tool_call_id, _new_id("tool_attempt"),
            self.attempt_number + 1)


@dataclass(frozen=True)
class ConfigurationSnapshot:
    """Immutable, allowlisted configuration metadata associated with a trace."""

    snapshot_id: str
    provider_ids: tuple[tuple[str, str], ...]
    model_ids: tuple[tuple[str, str], ...]
    prompt_revision_id: str
    endpointing_settings: tuple[tuple[str, bool | float], ...]
    behavior_settings: tuple[tuple[str, bool | float | int], ...]
    seed: int | None
    hardware_class: str
    cpu_architecture: str
    os_version: str
    execution_engine_id: str
    source_revision: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "provider_ids": dict(self.provider_ids),
            "model_ids": dict(self.model_ids),
            "prompt_revision_id": self.prompt_revision_id,
            "endpointing_settings": dict(self.endpointing_settings),
            "behavior_settings": dict(self.behavior_settings),
            "seed": self.seed,
            "hardware_class": self.hardware_class,
            "cpu_architecture": self.cpu_architecture,
            "os_version": self.os_version,
            "execution_engine_id": self.execution_engine_id,
            "source_revision": self.source_revision,
        }


def configuration_snapshot(config: Any, *, prompt_revision_id: str,
                           source_revision: str | None = None
                           ) -> ConfigurationSnapshot:
    """Build a safe snapshot from the repository's frozen ``Config`` object."""
    if not is_dataclass(config):
        raise TypeError("configuration snapshots require a dataclass instance")
    if not _safe_id(prompt_revision_id):
        raise ValueError("prompt_revision_id must be a safe identifier")
    if source_revision is not None and not _safe_id(source_revision):
        raise ValueError("source_revision must be a safe identifier")
    values = {f.name: getattr(config, f.name) for f in fields(config)}
    from factory import component_identity
    selected = component_identity(config)
    provider_ids = (("llm", selected["llm_provider"]),
                    ("stt", selected["stt_engine"]),
                    ("tts", selected["tts_engine"]))
    model_ids = (("llm", selected["llm_model"]),
                 ("stt", selected["stt_model"]),
                 ("tts", selected["tts_voice"]))
    for _, value in provider_ids + model_ids:
        if not _safe_attribute_value("gen_ai.request.model", value):
            raise ValueError("provider/model/voice identifiers must be safe")
    endpointing = tuple(sorted({
        "stt_ttfs_p99": values["stt_ttfs_p99"],
        "use_smart_turn": values["use_smart_turn"],
        "user_speech_timeout": values["user_speech_timeout"],
        "vad_stop_secs": values["vad_stop_secs"],
        "wait_for_transcript": values["wait_for_transcript"],
    }.items()))
    behavior = tuple(sorted({
        "filter_incomplete_user_turns": values["filter_incomplete_user_turns"],
        "input_sample_rate": values["input_sample_rate"],
        "llm_max_tokens": values["llm_max_tokens"],
        "llm_temperature": values["llm_temperature"],
        "output_sample_rate": values["output_sample_rate"],
        "vad_min_volume": values["vad_min_volume"],
    }.items()))
    mac = platform.mac_ver()[0]
    os_version = ".".join(mac.split(".")[:2]) if mac else "unsupported"
    payload = {
        "provider_ids": provider_ids,
        "model_ids": model_ids,
        "prompt_revision_id": prompt_revision_id,
        "endpointing_settings": endpointing,
        "behavior_settings": behavior,
        "seed": values["llm_seed"],
        "hardware_class": "personal_computer",
        "cpu_architecture": platform.machine().lower() or "unknown",
        "os_version": os_version,
        "execution_engine_id": "pipecat",
        "source_revision": source_revision,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=list).encode()).hexdigest()[:24]
    return ConfigurationSnapshot(
        f"config_{digest}", provider_ids, model_ids, prompt_revision_id,
        endpointing, behavior, values["llm_seed"], "personal_computer",
        payload["cpu_architecture"], os_version, "pipecat", source_revision,
    )


_BLOCKED_KEY_PARTS = frozenset({
    "audio", "transcript", "prompt", "instruction", "input", "output",
    "content", "text", "argument", "result", "payload", "exception",
    "stacktrace", "stack_trace", "traceback", "api_key", "secret",
    "credential", "password", "token_value", "hostname", "username",
    "user_name", "serial", "mac_address", "network", "device_id",
    "device_name", "file_path", "absolute_path", "environment",
})
_SAFE_KEYS = frozenset({
    "schema_version", "mode", "fixture", "workload_fixture_id",
    "configuration_snapshot_id", "strategy", "measured_as", "stream",
    "is_final", "language", "tool.name", "tool.state", "tool.attempt_number",
    "tool.progress_code", "error.classification", "turn.outcome",
    "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.operation.name",
    "gen_ai.output.type", "voice_id", "settings.voice", "settings.language",
    "settings.model", "settings.engine", "settings.temperature",
    "settings.no_speech_prob", "param.model", "param.seed",
    "param.temperature", "param.max_tokens", "vad.stop_secs", "wait_ms",
    "duration_ms", "metrics.ttfb", "metrics.audio_seconds",
    "metrics.character_count", "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens", "tts.interrupted",
    "telemetry.schema_version", "telemetry.privacy_mode",
    "telemetry.session_id", "telemetry.conversation_id",
    "telemetry.configuration_snapshot_id", "telemetry.workload_fixture_id",
    "telemetry.config.prompt_revision_id",
    "telemetry.config.cpu_architecture", "telemetry.config.os_version",
    "telemetry.config.execution_engine_id",
    "telemetry.workload_comparable",
})
_ABS_PATH = re.compile(r"(?<![\w.])(?:/[\w.@+~-]+){2,}")
_CREDENTIAL = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|password|secret)\s*[:= ]\s*\S+")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_URL = re.compile(r"(?i)\b(?:https?|ftp)://")
_WINDOWS_PATH = re.compile(r"(?i)(?:\b[A-Z]:\\|\\\\[^\\]+\\)")
_HOME_PATH = re.compile(r"(?:^|\s)~[/\\]")
_HOSTNAME = re.compile(
    r"(?i)\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|local|internal|test)\b")
_RAW_ERROR = re.compile(r"(?i)(?:traceback|exception|error:\s|failed\s+at)")

_NUMERIC_KEYS = frozenset({
    "tool.attempt_number", "settings.temperature", "settings.no_speech_prob",
    "param.seed", "param.temperature", "param.max_tokens", "vad.stop_secs",
    "wait_ms", "duration_ms", "metrics.ttfb", "metrics.audio_seconds",
    "metrics.character_count", "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
})
_BOOLEAN_KEYS = frozenset({
    "stream", "is_final", "tts.interrupted", "telemetry.workload_comparable",
})
_CODE_KEYS = frozenset({
    "tool.name", "tool.state", "tool.progress_code", "error.classification",
    "turn.outcome", "strategy", "measured_as", "mode",
    "telemetry.privacy_mode",
})
_IDENTIFIER_KEYS = frozenset({
    "fixture", "workload_fixture_id", "configuration_snapshot_id",
    "telemetry.schema_version", "telemetry.session_id",
    "telemetry.conversation_id", "telemetry.configuration_snapshot_id",
    "telemetry.workload_fixture_id", "telemetry.config.prompt_revision_id",
    "telemetry.config.cpu_architecture", "telemetry.config.os_version",
    "telemetry.config.execution_engine_id", "language", "settings.language",
})
_MODEL_KEYS = frozenset({
    "gen_ai.provider.name", "gen_ai.request.model", "gen_ai.operation.name",
    "gen_ai.output.type", "voice_id", "settings.voice", "settings.model",
    "settings.engine", "param.model",
})


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_ID_PATTERN.fullmatch(value))


def _unsafe_text(value: str) -> bool:
    return bool(_EMAIL.search(value) or _URL.search(value)
                or _ABS_PATH.search(value) or _WINDOWS_PATH.search(value)
                or _HOME_PATH.search(value) or _CREDENTIAL.search(value)
                or _HOSTNAME.search(value) or _RAW_ERROR.search(value))


def _token_shaped_model_id(value: str) -> bool:
    """Reject credentials where a human-readable model identifier is expected."""
    # ``+`` and padding distinguish whole-value standard base64 from ordinary
    # provider/model names, whose one slash is part of the public ID grammar.
    if (_MODEL_BASE64_TOKEN_PATTERN.fullmatch(value)
            and ("+" in value or "=" in value)):
        return True
    for segment in value.split("/"):
        if segment.lower().startswith(_MODEL_SECRET_PREFIXES):
            return True
        if _MODEL_JWT_PATTERN.fullmatch(segment):
            return True
        if len(segment) >= 32 and (
                _MODEL_HEX_TOKEN_PATTERN.fullmatch(segment)
                or _MODEL_BASE64_TOKEN_PATTERN.fullmatch(segment)):
            return True
    return False


def _safe_attribute_value(key: str, value: Any) -> bool:
    if key in _NUMERIC_KEYS:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if key in _BOOLEAN_KEYS:
        return isinstance(value, bool)
    if not isinstance(value, str) or _unsafe_text(value):
        return False
    if key in _CODE_KEYS:
        return bool(CONTROLLED_CODE_PATTERN.fullmatch(value))
    if key in _IDENTIFIER_KEYS:
        return _safe_id(value)
    if key in _MODEL_KEYS:
        return (len(value) <= MODEL_ID_MAX_LENGTH
                and bool(MODEL_ID_PATTERN.fullmatch(value))
                and ".." not in value and "://" not in value
                and not _token_shaped_model_id(value))
    return False


def is_sensitive_key(key: str) -> bool:
    if key in _SAFE_KEYS:
        return False
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _BLOCKED_KEY_PARTS)


def redact_text(value: str) -> str:
    """Redact recognizable credentials and absolute paths in diagnostic text."""
    value = _CREDENTIAL.sub("[REDACTED]", value)
    return _ABS_PATH.sub("[REDACTED]", value)


def filter_metadata(attributes: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only controlled, scalar metadata safe to persist or display."""
    if not isinstance(attributes, Mapping):
        return {}
    clean: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        key = str(key)
        if is_sensitive_key(key):
            continue
        if key not in _SAFE_KEYS:
            continue
        try:
            if _safe_attribute_value(key, value):
                clean[key] = redact_text(value) if isinstance(value, str) else value
        except Exception:
            continue
    return clean


def safe_span(span: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a span while enforcing the metadata-only persistence boundary."""
    if not isinstance(span, Mapping):
        return {"name": "unclassified", "trace_id": None, "span_id": None,
                "parent_span_id": None, "start_time_ns": None,
                "end_time_ns": None, "duration_ms": None, "attributes": {}}
    name = span.get("name")
    allowed = {
        "name": (name if isinstance(name, str)
                 and CONTROLLED_CODE_PATTERN.fullmatch(name) else "unclassified"),
        "trace_id": span.get("trace_id") if _safe_id(span.get("trace_id")) else None,
        "span_id": span.get("span_id") if _safe_id(span.get("span_id")) else None,
        "parent_span_id": (span.get("parent_span_id")
                           if span.get("parent_span_id") is None
                           or _safe_id(span.get("parent_span_id")) else None),
        "start_time_ns": span.get("start_time_ns")
        if isinstance(span.get("start_time_ns"), int) else None,
        "end_time_ns": span.get("end_time_ns")
        if isinstance(span.get("end_time_ns"), int) else None,
        "duration_ms": span.get("duration_ms")
        if isinstance(span.get("duration_ms"), (int, float)) else None,
    }
    allowed["attributes"] = filter_metadata(span.get("attributes"))
    return allowed


class ContractError(ValueError):
    pass


def validate_event(event: Mapping[str, Any]) -> None:
    """Validate one v1 event. Unknown versions fail closed."""
    if not isinstance(event, Mapping):
        raise ContractError("event must be an object")
    allowed_top = {"schema_version", "event_id", "event_name", "timestamp_ns",
                   "identities", "attributes"}
    if set(event) != allowed_top:
        raise ContractError("event has missing or unknown top-level keys")
    version = event.get("schema_version")
    if not isinstance(version, str) or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ContractError(f"unsupported schema_version: {version!r}")
    if (not isinstance(event.get("event_name"), str)
            or event.get("event_name") not in EVENT_NAMES):
        raise ContractError(f"unknown event_name: {event.get('event_name')!r}")
    if (not isinstance(event.get("timestamp_ns"), int)
            or isinstance(event.get("timestamp_ns"), bool)
            or event["timestamp_ns"] < 0):
        raise ContractError("timestamp_ns must be a non-negative integer")
    if not _safe_id(event.get("event_id")):
        raise ContractError("event_id must be a safe identifier")
    identities = event.get("identities")
    if not isinstance(identities, Mapping):
        raise ContractError("identities must be an object")
    if set(identities) != set(_ID_FIELDS):
        raise ContractError("identities has missing or unknown keys")
    for name in _REQUIRED_IDS:
        if not _safe_id(identities.get(name)):
            raise ContractError(f"identities.{name} must be a safe identifier")
    for name in ("parent_span_id", "logical_tool_call_id", "tool_attempt_id"):
        if identities.get(name) is not None and not _safe_id(identities.get(name)):
            raise ContractError(f"identities.{name} must be null or a safe identifier")
    attrs = event.get("attributes")
    if not isinstance(attrs, Mapping):
        raise ContractError("attributes must be an object")
    name = event["event_name"]
    if name in TOOL_EVENTS:
        for field in ("logical_tool_call_id", "tool_attempt_id"):
            if not identities.get(field):
                raise ContractError(f"tool event requires identities.{field}")
        if (not isinstance(attrs.get("tool.attempt_number"), int)
                or isinstance(attrs.get("tool.attempt_number"), bool)
                or attrs.get("tool.attempt_number") < 1):
            raise ContractError("tool event requires integer tool.attempt_number")
    rejected = [key for key in attrs if is_sensitive_key(str(key))]
    if rejected:
        raise ContractError(f"sensitive attribute keys: {sorted(rejected)}")
    for controlled in ("tool.progress_code", "error.classification"):
        value = attrs.get(controlled)
        if (value is not None
                and (not isinstance(value, str)
                     or not CONTROLLED_CODE_PATTERN.fullmatch(value))):
            raise ContractError(f"{controlled} must be a controlled code")
    if filter_metadata(attrs) != dict(attrs):
        raise ContractError("attributes contain unapproved metadata")


_ORDERED_BOUNDARIES = (
    ("user_speech.started", "user_speech.ended"),
    ("endpointing.started", "endpointing.resolved"),
    ("stt.started", "stt.first"),
    ("stt.first", "stt.final"),
    ("llm.started", "llm.first_token"),
    ("tts.started", "tts.first_synthesized_sample"),
    ("tts.first_synthesized_sample", "output_transport.first_audio"),
)


def validate_trace(events: Iterable[Mapping[str, Any]]) -> None:
    """Validate turn identity, span hierarchy, boundaries, and tool attempts."""
    items = list(events)
    if not items:
        raise ContractError("trace must contain events")
    for event in items:
        validate_event(event)
    event_ids = [event["event_id"] for event in items]
    if len(event_ids) != len(set(event_ids)):
        raise ContractError("event_id values must be unique")
    stable = ("session_id", "conversation_id", "turn_id", "trace_id",
              "configuration_snapshot_id", "workload_fixture_id")
    first = items[0]["identities"]
    for event in items[1:]:
        for field in stable:
            if event["identities"][field] != first[field]:
                raise ContractError(f"inconsistent {field}")
    by_name: dict[str, list[int]] = {}
    for event in items:
        by_name.setdefault(event["event_name"], []).append(event["timestamp_ns"])
    terminals = sum(len(by_name.get(name, [])) for name in TURN_TERMINALS)
    if terminals != 1:
        raise ContractError("trace requires exactly one terminal turn event")
    terminal_time = max(by_name[name][0] for name in TURN_TERMINALS if name in by_name)
    if any(event["timestamp_ns"] > terminal_time for event in items):
        raise ContractError("events cannot occur after the terminal turn event")
    for before, after in _ORDERED_BOUNDARIES:
        if after in by_name and before not in by_name:
            raise ContractError(f"{after} requires {before}")
        if before in by_name and after in by_name:
            if min(by_name[after]) < min(by_name[before]):
                raise ContractError(f"{after} precedes {before}")

    spans = {event["identities"]["span_id"] for event in items}
    parents: dict[str, str | None] = {}
    for event in items:
        span = event["identities"]["span_id"]
        parent = event["identities"].get("parent_span_id")
        if parent is not None and parent not in spans:
            raise ContractError(f"missing parent span: {parent}")
        if parent == event["identities"]["span_id"]:
            raise ContractError("a span cannot parent itself")
        if span in parents and parents[span] != parent:
            raise ContractError("a span cannot change parents")
        parents[span] = parent
    for span in spans:
        seen = {span}
        parent = parents.get(span)
        while parent is not None:
            if parent in seen:
                raise ContractError("span parent cycle")
            seen.add(parent)
            parent = parents.get(parent)

    attempts: dict[str, dict[str, Any]] = {}
    logical_numbers: dict[str, set[int]] = {}
    numbered_attempts: dict[tuple[str, int], str] = {}
    for event in items:
        if event["event_name"] not in TOOL_EVENTS:
            continue
        ids, attrs = event["identities"], event["attributes"]
        attempt_id = ids["tool_attempt_id"]
        logical_id = ids["logical_tool_call_id"]
        number = attrs["tool.attempt_number"]
        attempt = attempts.setdefault(attempt_id, {
            "logical": logical_id, "number": number, "states": []})
        if attempt["logical"] != logical_id or attempt["number"] != number:
            raise ContractError("tool attempt identity changed")
        numbered = (logical_id, number)
        if numbered in numbered_attempts and numbered_attempts[numbered] != attempt_id:
            raise ContractError("tool attempt number must identify one attempt")
        numbered_attempts[numbered] = attempt_id
        attempt["states"].append((event["timestamp_ns"], event["event_name"]))
        logical_numbers.setdefault(logical_id, set()).add(number)
    for attempt in attempts.values():
        states = [state for _, state in sorted(
            attempt["states"], key=lambda item: item[0])]
        if not states or states[0] != "tool.requested":
            raise ContractError("tool attempt must start with tool.requested")
        if len(states) < 3 or states[1] != "tool.running":
            raise ContractError("tool.requested must be followed by tool.running")
        if states[-1] not in {"tool.completed", "tool.error", "tool.cancelled"}:
            raise ContractError("tool attempt requires a terminal state")
        if any(state != "tool.progress" for state in states[2:-1]):
            raise ContractError("only tool.progress may occur while running")
    for numbers in logical_numbers.values():
        if numbers != set(range(1, max(numbers) + 1)):
            raise ContractError("tool attempt numbers must be contiguous from 1")


def load_jsonl(path: Any) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
