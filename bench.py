"""Run one fixture through both builds and write the traces.

    uv run python bench.py --fixture 02-medium --reps 3

Both builds get identical audio, identical models, identical config. The only
difference is scheduling. The first rep of each build is discarded: the first
inference pays model load and graph compilation and is not representative of
anything.
"""

import argparse
import asyncio
import sys
from pathlib import Path

from loguru import logger
from opentelemetry import trace

from config import CONFIG
from tracing_setup import init_tracing


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", default="02-medium")
    ap.add_argument("--reps", type=int, default=CONFIG.measured_reps)
    ap.add_argument("--traces", default="artifacts/reference-traces.jsonl")
    ap.add_argument("--stop-secs", type=float, default=CONFIG.vad_stop_secs)
    ap.add_argument("--only", choices=["naive", "streaming"])
    args = ap.parse_args()

    wav = f"fixtures/{args.fixture}.wav"
    if not Path(wav).exists():
        raise SystemExit(f"no fixture at {wav}. Run `make fixtures`, "
                         f"or `uv run python record.py --name {args.fixture}`.")

    traces = Path(args.traces)
    traces.unlink(missing_ok=True)
    init_tracing("voice-infer", traces)

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<dim>{time:HH:mm:ss}</dim> {message}")

    import naive
    import streaming

    total = args.reps + CONFIG.warmup_reps
    modes = [args.only] if args.only else ["naive", "streaming"]

    for mode in modes:
        for rep in range(total):
            warm = rep < CONFIG.warmup_reps
            logger.info(f"{mode:<10} rep {rep + 1}/{total}"
                        f"{'  (warmup, discarded)' if warm else ''}")
            if mode == "naive":
                await naive.run(wav)
            else:
                await streaming.one_run(rep, wav, args.stop_secs)

    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()

    print(f"\n  traces -> {traces}")
    print(f"  now run:  uv run python budget.py --traces {traces}")


if __name__ == "__main__":
    asyncio.run(main())
