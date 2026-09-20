"""Which settings the observability page may change, and how far.

The page can nudge a handful of latency thresholds and watch the waterfall
respond. Everything else in `Config` (models, engines, prompts, audio devices,
paths) stays out of reach: this module is an allowlist, not a mirror of `Config`.

A value is only accepted if it has the right type and sits inside the range
declared here. Unknown names are rejected rather than ignored, so a typo cannot
look like it worked. The result is a new frozen `Config` built with
`dataclasses.replace`; nothing is mutated, and the launch configuration is kept
so the page can show what a setting started as.

Changes apply the next time the pipeline is built, because Pipecat's endpointing
strategy takes these as constructor arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields, replace
from typing import Any, Mapping

from voice_agent.config import Config


class TuningError(ValueError):
    """A rejected change. `code` is a fixed string, safe to send to the browser."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Tunable:
    key: str
    label: str
    kind: str                      # "number" | "integer" | "boolean"
    about: str                     # what moving it does, in one plain sentence
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    unit: str | None = None
    warn_below: float | None = None
    warn_above: float | None = None
    warning: str | None = None     # shown only when the value crosses a warn threshold


TUNABLES: tuple[Tunable, ...] = (
    Tunable(
        "vad_stop_secs", "VAD silence window", "number",
        "Silence the voice-activity detector needs before it decides you have "
        "stopped talking. Lower ends your turn sooner.",
        minimum=0.1, maximum=2.0, step=0.05, unit="s", warn_below=0.3,
        warning="Below about 0.3 s a natural pause can split one question into two turns."),
    Tunable(
        "user_speech_timeout", "Speech timeout", "number",
        "Extra time after the VAD stops in which you may keep talking before "
        "the turn is closed.",
        minimum=0.0, maximum=2.0, step=0.05, unit="s"),
    Tunable(
        "stt_ttfs_p99", "STT safety timer", "number",
        "The longest the turn will wait for the transcript. The wait ends the "
        "moment the transcript is reported final, so this only changes anything "
        "when it is not.",
        minimum=0.05, maximum=2.0, step=0.05, unit="s", warn_below=0.1,
        warning="Below about 0.1 s the turn can close before a slower transcript arrives."),
    Tunable(
        "wait_for_transcript", "Wait for transcript", "boolean",
        "Also require a transcript before the turn can close."),
    Tunable(
        "use_smart_turn", "Smart-turn model", "boolean",
        "Let a small model decide from the audio whether you sound finished, "
        "instead of a silence timer.",
        warning="The model loads on the first turn, so that turn is slower."),
    Tunable(
        "llm_max_tokens", "Reply length cap", "integer",
        "Caps how long the reply can run. It mostly changes how long the reply "
        "takes to generate and speak, not when the first audio starts.",
        minimum=10, maximum=300, step=1, unit="tokens", warn_below=20,
        warning="Below about 20 tokens replies may be cut off mid-sentence."),
)

_BY_KEY = {t.key: t for t in TUNABLES}

# Every tunable must be a real `Config` field, or `replace` would fail late.
_CONFIG_FIELDS = {f.name for f in fields(Config)}
assert set(_BY_KEY) <= _CONFIG_FIELDS, "tuning names a setting Config does not have"


def _validate_one(tunable: Tunable, value: Any) -> Any:
    if tunable.kind == "boolean":
        if not isinstance(value, bool):
            raise TuningError("invalid_value")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TuningError("invalid_value")
    if not math.isfinite(value):
        raise TuningError("invalid_value")
    if tunable.kind == "integer":
        if isinstance(value, float):
            if not value.is_integer():
                raise TuningError("invalid_value")
            value = int(value)
    else:
        value = round(float(value), 3)
    if not tunable.minimum <= value <= tunable.maximum:
        raise TuningError("out_of_range")
    return value


def validate(values: Any) -> dict[str, Any]:
    """The accepted overrides, or a `TuningError` with a fixed code."""
    if not isinstance(values, Mapping):
        raise TuningError("invalid_value")
    clean: dict[str, Any] = {}
    for key, value in values.items():
        tunable = _BY_KEY.get(key) if isinstance(key, str) else None
        if tunable is None:
            raise TuningError("unknown_setting")
        clean[key] = _validate_one(tunable, value)
    return clean


def apply(base: Config, values: Mapping[str, Any]) -> Config:
    """`base` with the validated overrides applied. `base` is never changed."""
    return replace(base, **validate(values))


def describe(base: Config, current: Config) -> list[dict[str, Any]]:
    """The panel's contents: each setting, its limits, its launch and current value."""
    out = []
    for t in TUNABLES:
        item: dict[str, Any] = {
            "key": t.key, "label": t.label, "kind": t.kind, "about": t.about,
            "launch": getattr(base, t.key), "value": getattr(current, t.key),
        }
        for name in ("minimum", "maximum", "step", "unit", "warn_below",
                     "warn_above", "warning"):
            value = getattr(t, name)
            if value is not None:
                item[name] = value
        out.append(item)
    return out
