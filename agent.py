"""The pipeline. One definition, two ways to run it.

Pipecat is the orchestrator. This file is the wiring: which stages exist, in
what order, and what policy decides that your turn is over. `live.py` runs it
against this machine's microphone; `main()` below runs the same pipeline
against a WAV file for anyone whose audio setup is awkward, or who wants a turn
they can replay.

    uv run python agent.py fixtures/02-medium.wav     # or: make trace

Both paths build the pipeline here, so there is one answer to "what does this
agent actually do", and the trace from either is read by the same `budget.py`.

    transport.input  ─►  stt  ─►  context  ─►  llm  ─►  tts  ─►  transport.output

The interesting part is not the list. It is that the user aggregator in the
middle is holding the whole thing back: nothing downstream of it starts until
the turn strategy decides you have stopped talking.
"""

import asyncio
import sys
import time
from pathlib import Path

from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import factory
from config import CONFIG
from tracing_setup import TurnSpanObserver, init_tracing
from wav_transport import Capture, WavFileTransport

TRACES = Path("artifacts/my-traces.jsonl")


def build_pipeline(transport, *, mute_while_bot_speaks: bool = False) -> Pipeline:
    """The four stages, from config, in the order a turn travels them."""
    stt, llm, tts = factory.make_stt(), factory.make_llm(), factory.make_tts()

    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=factory.make_vad(),
            # Endpointing stated explicitly rather than left to the default.
            # Pipecat's stop strategy runs two timers in parallel -- a pause
            # policy, and a safety net sized to STT's P99 latency which falls
            # back to a conservative 1.0 s for local models -- and on a local
            # stack that is roughly 600 ms of waiting for a transcript that
            # has already arrived. See config.py: both are set honestly here.
            user_turn_strategies=UserTurnStrategies(
                stop=[
                    TurnAnalyzerUserTurnStopStrategy(
                        turn_analyzer=LocalSmartTurnAnalyzerV3()
                    )
                    if CONFIG.use_smart_turn
                    else SpeechTimeoutUserTurnStopStrategy(
                        user_speech_timeout=CONFIG.user_speech_timeout,
                        wait_for_transcript=CONFIG.wait_for_transcript,
                    )
                ]
            ),
            # THE ECHO FIX, for the microphone path only. One machine's
            # microphone and speakers with no acoustic echo cancellation means
            # the agent hears its own voice, voice activity treats it as you
            # speaking, the reply is interrupted mid-sentence, and its own
            # words come back through speech-to-text as your next question.
            # Observed for real: the context filled with the bot answering
            # itself. A WAV run has no speaker, so it does not need this.
            user_mute_strategies=[AlwaysUserMuteStrategy()]
            if mute_while_bot_speaks
            else [],
            # NOT using filter_incomplete_user_turns: measured, it costs an
            # 860-token classifier call plus a ~5 s wait for speech that never
            # arrives. It treats the symptom anyway; the cause is voice
            # activity ending your turn on a clause pause.
        ),
    )

    return Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        assistant_agg,
    ])


def build_worker(pipeline, *, mode: str, fixture: str | None = None,
                 conversation_id: str = "live", observers=()) -> PipelineWorker:
    """Wrap the pipeline in a worker that traces itself.

    `enable_tracing` is what makes Pipecat emit the `stt`, `llm` and `tts`
    spans; `enable_turn_tracking` is what wraps them in a `turn`. Everything
    the budget and the live page report comes from these, plus the two spans
    TurnSpanObserver adds.
    """
    return PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[TurnSpanObserver(mode=mode, fixture=fixture), *observers],
        enable_tracing=True,
        enable_turn_tracking=True,
        conversation_id=conversation_id,
        additional_span_attributes={
            "mode": mode,
            "stack": factory.describe(),
            **({"fixture": fixture} if fixture else {}),
        },
    )


# ------------------------------------------------------ without a microphone -

async def one_run(rep: int, wav: str) -> Capture:
    """Feed one WAV through the pipeline at real 20 ms cadence."""
    capture = Capture()
    params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=CONFIG.input_sample_rate,
        audio_out_sample_rate=CONFIG.output_sample_rate,
    )
    transport = WavFileTransport(params, wav, capture,
                                 trailing_silence_s=CONFIG.trailing_silence_s)

    pipeline = build_pipeline(transport)
    worker = build_worker(pipeline, mode="file", fixture=Path(wav).stem,
                          conversation_id=f"file-{rep}")

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def watchdog():
        """End once the bot has spoken and gone quiet, or on a hard timeout."""
        start = time.monotonic()
        while time.monotonic() - start < 90:
            await asyncio.sleep(0.25)
            if capture.t_first_audio and capture.out_chunks:
                last = capture.out_chunks[-1][0]
                if time.monotonic() - last > 2.0:
                    logger.info("watchdog: bot finished speaking")
                    return
        logger.warning("watchdog: hard timeout")

    wd = asyncio.create_task(watchdog())
    run = asyncio.create_task(runner.run())
    await wd
    await runner.cancel()
    try:
        await asyncio.wait_for(run, timeout=15)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    return capture


async def main(wav: str, reps: int, traces: Path):
    traces.unlink(missing_ok=True)
    ok, _ = init_tracing("voice-infer-file", traces)
    logger.info(f"tracing initialized: {ok}")

    results = []
    for rep in range(reps):
        # The first inference pays model load and graph compilation and is
        # several times slower than the rest. It is run, reported, and then
        # ignored, rather than quietly not run at all.
        logger.info(f"--- rep {rep} {'(cold, ignore this one)' if rep == 0 else ''} ---")
        results.append(await one_run(rep, wav))

    from opentelemetry import trace
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()

    print()
    print(f"  {Path(wav).name}, {reps} reps through the pipeline")
    # e2e_ms is measured at the speaker, where the transport writes the first
    # chunk. The span window ends slightly earlier, when the first synthesized
    # sample exists; the gap between the two is the output buffer.
    print(f"  {'rep':<6}{'e2e_ms':>10}{'chunks':>9}{'gap_p50':>10}{'drift_ms':>10}")
    for i, c in enumerate(results):
        e2e = c.e2e_speech_end_to_first_audio
        st = c.cadence_stats()
        tag = " (cold)" if i == 0 else ""
        print(f"  {i:<6}{(e2e * 1000 if e2e else -1):>10.0f}{st.get('chunks',0):>9}"
              f"{st.get('gap_ms_p50',0):>10.2f}{st.get('cumulative_drift_ms',0):>10.1f}{tag}")
    print(f"\n  spans written to {traces}")
    print(f"  uv run python budget.py --traces {traces}\n")
    return 0 if any(c.e2e_speech_end_to_first_audio for c in results) else 1


if __name__ == "__main__":
    import argparse

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("wav", nargs="?", default="fixtures/02-medium.wav")
    ap.add_argument("--reps", type=int, default=2,
                    help="the first one is cold and is reported as such")
    ap.add_argument("--traces", type=Path, default=TRACES)
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.wav, args.reps, args.traces)))
