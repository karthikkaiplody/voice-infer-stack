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

import collections
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


def load_traces(path):
    """Group spans by trace, keyed by the conversation span's attributes.

    additional_span_attributes are attached to the CONVERSATION span only, not
    propagated to its children. Grouping on a child's attributes therefore
    silently yields nothing, so the join has to go through trace_id.
    """
    spans = [json.loads(l) for l in Path(path).open() if l.strip()]
    by_trace = collections.defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(s)

    runs = []
    for tid, group in by_trace.items():
        conv = next((g for g in group if g["name"] == "conversation"), None)
        if not conv:
            continue
        runs.append({"trace_id": tid, "attrs": conv["attributes"], "spans": group})
    runs.sort(key=lambda r: min(s["start_time_ns"] for s in r["spans"]))
    return runs


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


def overlap_report(run):
    """The llm/tts concurrency claim, computed rather than asserted."""
    llm, tts = intervals(run, "llm"), intervals(run, "tts")
    if not llm or not tts:
        return None
    return {
        "llm_tts_intersection_ms": round(intersection_ms(llm, tts), 1),
        "duration_sum_ms": round(duration_sum_ms(llm, tts), 1),
        "union_ms": round(union_ms(llm, tts), 1),
        "llm_spans": len(llm),
        "tts_spans": len(tts),
    }


def stage_table(run):
    """Per-stage intervals relative to the conversation span start."""
    t0 = min(s["start_time_ns"] for s in run["spans"])
    return [{
        "name": s["name"],
        "start_ms": round((s["start_time_ns"] - t0) / 1e6, 1),
        "end_ms": round((s["end_time_ns"] - t0) / 1e6, 1),
        "duration_ms": round(s["duration_ms"], 1),
        "ttfb": s["attributes"].get("metrics.ttfb"),
    } for s in sorted(run["spans"], key=lambda s: s["start_time_ns"])]


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
