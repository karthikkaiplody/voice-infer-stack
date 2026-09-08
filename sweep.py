"""Sweep the VAD silence timeout and measure what it costs.

This is the experiment behind the talk's title. `stop_secs` is how long the
agent waits in silence before deciding you have finished speaking. Set it below
a speaker's natural pause and the agent answers half a sentence, throws the work
away, and answers again. Set it high and every turn pays that wait in full.

Every row is real runs. Nothing is inferred, nothing is derived by arithmetic
from another row.
"""

import asyncio
import json
from pathlib import Path

from loguru import logger
from opentelemetry import trace

import analysis
import streaming
from config import CONFIG
from tracing_setup import init_tracing

TRACES = Path("artifacts/sweep-traces.jsonl")
RESULTS = Path("artifacts/sweep-results.json")
STOP_SECS = [0.2, 0.3, 0.5, 0.8]
FIXTURES = ["fixtures/02-medium.wav", "fixtures/03-trailing-pause.wav"]


def flush():
    """OTel installs a BatchSpanProcessor, so spans are buffered. Reading the
    JSONL without flushing races the exporter and silently undercounts."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()


async def main():
    TRACES.unlink(missing_ok=True)
    init_tracing("voice-infer-sweep", TRACES)
    logger.remove()

    manifest = {m["name"]: m for m in json.load(open("fixtures/manifest.json"))}

    print(f"warming up ({CONFIG.warmup_reps} rep, discarded)...", flush=True)
    for _ in range(CONFIG.warmup_reps):
        await streaming.one_run(-1, FIXTURES[0], 0.5)

    cells = []
    for wav in FIXTURES:
        for ss in STOP_SECS:
            e2es, failures = [], 0
            for rep in range(CONFIG.measured_reps):
                c = await streaming.one_run(rep, wav, ss)
                e = c.e2e_speech_end_to_first_audio
                if e:
                    e2es.append(e * 1000)
                else:
                    failures += 1
            cells.append({
                "fixture": Path(wav).stem, "stop_secs": ss,
                "e2e_ms": analysis.median(e2es), "e2e_min_ms": min(e2es) if e2es else None,
                "e2e_max_ms": max(e2es) if e2es else None,
                "n_ok": len(e2es), "n_failed": failures,
                "n_attempted": CONFIG.measured_reps,
            })
            print(f"  {Path(wav).stem:20s} stop_secs={ss:<5} "
                  f"e2e={cells[-1]['e2e_ms'] or -1:7.0f}ms  n={len(e2es)}/{CONFIG.measured_reps}",
                  flush=True)

    flush()

    # Join spans back to their cell through trace_id, then compute the derived
    # quantities from the traces rather than from anything remembered in-process.
    runs = analysis.load_traces(TRACES)
    agg = {}
    for r in runs:
        a = r["attrs"]
        if a.get("rep") == -1:
            continue
        key = (a.get("fixture"), a.get("vad.stop_secs"))
        if None in key:
            continue
        d = agg.setdefault(key, {"runs": 0, "llm_calls": 0, "overlaps": []})
        d["runs"] += 1
        d["llm_calls"] += len(analysis.intervals(r, "llm"))
        ov = analysis.overlap_report(r)
        if ov:
            d["overlaps"].append(ov["llm_tts_intersection_ms"])

    for c in cells:
        key = (c["fixture"], c["stop_secs"])
        d = agg.get(key, {"runs": 0, "llm_calls": 0, "overlaps": []})
        c["llm_calls_per_run"] = round(d["llm_calls"] / d["runs"], 2) if d["runs"] else None
        c["llm_tts_overlap_ms"] = analysis.median(d["overlaps"]) if d["overlaps"] else None
        c["traced_runs"] = d["runs"]
        m = manifest[c["fixture"]]
        c["max_internal_pause_ms"] = m["max_internal_pause_ms"]
        c["min_workable_stop_secs"] = m["min_workable_stop_secs"]

    RESULTS.write_text(json.dumps(cells, indent=2) + "\n")

    print("\n" + "=" * 96)
    print("VAD SILENCE TIMEOUT SWEEP")
    print("M4 Pro 24GB / pipecat 1.8.1 / whisper-large-v3-turbo-q4 (MLX) + llama3.2:3b + kokoro")
    print("=" * 96)
    hdr = (f"  {'fixture':<20}{'pause':>7}{'stop_s':>8}{'e2e_ms':>9}{'range':>14}"
           f"{'n':>7}{'llm/run':>9}{'overlap':>9}   verdict")
    print(hdr)
    for c in cells:
        rng = (f"{c['e2e_min_ms']:.0f}-{c['e2e_max_ms']:.0f}"
               if c["e2e_min_ms"] is not None else "-")
        clean = (c["llm_calls_per_run"] or 0) <= 1.05
        below = c["stop_secs"] < c["min_workable_stop_secs"]
        verdict = "clean" if clean else "FALSE ENDPOINT"
        if below:
            verdict += " (stop_secs < pause)"
        n = f"{c['n_ok']}/{c['n_attempted']}"
        print(f"  {c['fixture']:<20}{c['max_internal_pause_ms']:>6.0f}m{c['stop_secs']:>8}"
              f"{c['e2e_ms'] or -1:>9.0f}{rng:>14}{n:>7}"
              f"{c['llm_calls_per_run'] or 0:>9.2f}"
              f"{(c['llm_tts_overlap_ms'] or 0):>8.0f}m   {verdict}")
    print("=" * 96)
    print("  e2e = end of user speech -> first synthesized audio byte buffered.")
    print("  Median of n successful runs. overlap = llm/tts span interval intersection.")


if __name__ == "__main__":
    asyncio.run(main())
