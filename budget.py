"""Where did the milliseconds go?

Prints the latency budget for one turn, per stage, as a share of the total.
Reads OpenTelemetry spans from a JSONL file. It does not import the pipeline,
so it works on traces from this repo or from any agent that emits the span
names in SPANS.md.

    uv run python budget.py                          # committed reference traces
    uv run python budget.py --traces mine.jsonl      # yours

The number that matters is not the total. It is which stage owns it, and how
much of the stage time is recovered by overlapping rather than by being faster.
"""

import argparse
from pathlib import Path

import analysis

# Stage order as the audience experiences it. Names match SPANS.md.
STAGES = ["turn_detection", "stt", "llm", "tts"]
WINDOW = "e2e.speech_end_to_first_audio"

# turn_detection is a WAIT, not work. STT transcribing buffered audio while the
# detector waits is not a scheduling win, so counting that intersection as
# "recovered by streaming" would inflate the headline. Overlap is only claimed
# between stages that actually compute.
COMPUTE = ["stt", "llm", "tts"]
BUDGET_MS = 800.0  # the human conversational window this is all measured against


def clip(ivs, window):
    """Keep only the part of each interval that falls inside the budget window."""
    lo, hi = window
    out = []
    for s, e in ivs:
        s2, e2 = max(s, lo), min(e, hi)
        if e2 > s2:
            out.append((s2, e2))
    return out


def load_spans(path):
    """All spans, flat. No trace grouping.

    Joining on time rather than trace_id is deliberate. It means a stage span
    only has to overlap the budget window to be counted, so this works on traces
    from a pipeline that structures its spans differently from ours, or emits no
    parent span at all.
    """
    import json
    return [json.loads(l) for l in Path(path).open() if l.strip()]


def windows(spans):
    """Each budget window: one turn, from speech end to first audio."""
    out = [s for s in spans if s["name"] == WINDOW]
    out.sort(key=lambda s: s["start_time_ns"])
    return out


def summarize(spans, win):
    """Reduce one turn to a budget.

    Every stage is clipped to the budget window, so time a stage spent running
    while the user was still speaking is not counted against the user's wait.
    Segmented STT always starts during user speech, so without clipping it looks
    like it dominates when most of its span was free.

    Overlap is computed by intersecting span intervals, never by comparing sums
    of durations. Two stages can both be long while running concurrently; only
    the intervals tell you whether they actually did.
    """
    window = (win["start_time_ns"], win["end_time_ns"])
    inside = {}
    raw_counts = {}
    for name in STAGES:
        ivs = [(s["start_time_ns"], s["end_time_ns"]) for s in spans
               if s["name"] == name]
        c = clip(ivs, window)
        if c:
            inside[name] = c
            raw_counts[name] = sum(
                1 for a, b in ivs if b > window[0] and a < window[1]
            )
    present = [s for s in STAGES if s in inside]
    if not present:
        return None

    compute_ivs = [iv for s in present if s in COMPUTE for iv in inside[s]]
    wall_ms = (window[1] - window[0]) / 1e6
    covered_ms = analysis.union_ms(compute_ivs)
    sum_ms = analysis.duration_sum_ms(compute_ivs)

    rows = [{"stage": n,
             "duration_ms": analysis.duration_sum_ms(inside[n]),
             "count": raw_counts[n]} for n in present]

    compute = [n for n in present if n in COMPUTE]
    pairs = []
    for i, a in enumerate(compute):
        for b in compute[i + 1:]:
            ov = analysis.intersection_ms(inside[a], inside[b])
            if ov > 1:
                pairs.append((a, b, ov))

    return {"rows": rows, "wall_ms": wall_ms, "covered_ms": covered_ms,
            "sum_ms": sum_ms, "recovered_ms": sum_ms - covered_ms,
            "overlaps": pairs, "attrs": win["attributes"]}


def render(summary, label):
    a = summary["attrs"]
    mode = a.get("mode", "unknown")
    fixture = a.get("fixture", "?")
    wall = summary["wall_ms"]

    print()
    print(f"  BUDGET  {fixture}  ·  {mode}  ·  {label}")
    print(f"  {'-' * 66}")
    print(f"  {'stage':<18}{'ms':>9}{'share':>9}   {'':<20}")
    for r in summary["rows"]:
        share = r["duration_ms"] / wall * 100 if wall else 0
        note = ""
        if r["stage"] == "turn_detection":
            note = "waiting, not computing"
        elif r["stage"] == "stt" and r["count"] == 1:
            note = "cannot overlap (segmented)"
        if r["count"] > 1:
            note = f"{r['count']}x - work was discarded"
        print(f"  {r['stage']:<18}{r['duration_ms']:>9.0f}{share:>8.0f}%   {note}")

    print(f"  {'-' * 66}")
    print(f"  {'compute, summed':<18}{summary['sum_ms']:>9.0f}")
    for a_name, b_name, ov in summary["overlaps"]:
        print(f"  {'  overlap ' + a_name + '/' + b_name:<18}{-ov:>9.0f}"
              f"{-ov / wall * 100:>8.0f}%   recovered by streaming")
    print(f"  {'wall clock':<18}{wall:>9.0f}")
    print()
    over = wall / BUDGET_MS
    print(f"  against the {BUDGET_MS:.0f} ms human window: {over:.1f}x over")
    if summary["recovered_ms"] > 1:
        pct = summary["recovered_ms"] / summary["sum_ms"] * 100
        print(f"  overlapping recovered {summary['recovered_ms']:.0f} ms "
              f"({pct:.0f}% of compute). Everything else is a floor.")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--traces", default="artifacts/reference-traces.jsonl",
                    help="JSONL of OpenTelemetry spans (see SPANS.md)")
    ap.add_argument("--mode", help="only this mode, e.g. naive or streaming")
    ap.add_argument("--skip-warmup", type=int, default=1,
                    help="drop the first N turns of each mode (cold models)")
    args = ap.parse_args()

    path = Path(args.traces)
    if not path.exists():
        raise SystemExit(f"no traces at {path}. Run `make bench` first, "
                         f"or pass --traces.")

    spans = load_spans(path)
    wins = windows(spans)
    if args.mode:
        wins = [w for w in wins if w["attributes"].get("mode") == args.mode]
    if not wins:
        raise SystemExit(
            f"no '{WINDOW}' spans in {path}. See SPANS.md for the contract.")

    by_mode = {}
    for w in wins:
        by_mode.setdefault(w["attributes"].get("mode", "?"), []).append(w)

    for mode, ws in by_mode.items():
        ws = ws[args.skip_warmup:] if len(ws) > args.skip_warmup else ws
        totals = [(w["end_time_ns"] - w["start_time_ns"]) / 1e6 for w in ws]
        s = summarize(spans, ws[-1])
        if s:
            render(s, f"median of {len(ws)} runs: "
                      f"{analysis.median(totals):.0f} ms")


if __name__ == "__main__":
    main()
