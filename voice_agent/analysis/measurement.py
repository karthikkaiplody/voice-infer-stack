"""Trace analysis. Every number the talk reports comes from here.

Two rules this module exists to enforce:

1. OVERLAP IS AN INTERVAL PROPERTY, NOT A DURATION PROPERTY.
   Summing span durations and comparing to end-to-end cannot distinguish
   "these two stages ran concurrently" from "one stage kept working after the
   first audio byte was already out". The only sound test is intersecting the
   time intervals. So that is what is computed here.

2. A STATISTIC IS REPORTED WITH ITS SAMPLE COUNT.
   `sorted(x)[len(x)//2]` is the upper-middle value, not the median, for any
   even-length sample. And silently dropping failed runs turns "4 reps" into an
   unlabelled 2. Both are handled below and both are reported.
"""

# Keeps this file runnable on the system Python that ships with macOS
# (3.9). budget.py and measurement.py are pure standard library on purpose:
# reading the committed traces should need no install at all.
from __future__ import annotations

import json
from pathlib import Path


def median(xs):
    """True median. For even n, the mean of the two middle values."""
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def intervals(run, name):
    """All [start_ns, end_ns] intervals for spans of one stage."""
    return [(s["start_time_ns"], s["end_time_ns"])
            for s in run["spans"] if s["name"] == name]


def intersection_ms(a, b):
    """Total overlapping time between two interval sets, in ms."""
    total = 0
    for s1, e1 in a:
        for s2, e2 in b:
            total += max(0, min(e1, e2) - max(s1, s2))
    return total / 1e6


def union_ms(*sets):
    """Wall-clock span covered by the union of interval sets, in ms."""
    ivs = sorted(i for s in sets for i in s)
    if not ivs:
        return 0.0
    total, cur_s, cur_e = 0, *ivs[0]
    for s, e in ivs[1:]:
        if s > cur_e:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    return (total + cur_e - cur_s) / 1e6


def duration_sum_ms(*sets):
    return sum(e - s for iv in sets for s, e in iv) / 1e6


def speech_end_ms(wav_path) -> float | None:
    """The measured end of speech for a fixture, from fixtures/manifest.json.

    This is t0 for every latency number in the repo. It is NOT the end of the
    WAV file: fixtures carry trailing silence (measured 100-190 ms), and using
    file duration as t0 silently understates end-to-end latency by that much.
    """
    import json
    from pathlib import Path

    manifest = Path(wav_path).parent / "manifest.json"
    if not manifest.exists():
        return None
    stem = Path(wav_path).stem
    for entry in json.loads(manifest.read_text()):
        if entry["name"] == stem:
            return float(entry["t_speech_end_ms"])
    return None
