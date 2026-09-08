"""BUILD 2 - the streaming voice agent, built on Pipecat.

Same models, same config, same audio as naive.py. The difference is that the
pipeline overlaps work: the LLM streams tokens, and TTS begins synthesizing the
first sentence while the rest is still being generated.

Two things this buys, and they are not the same thing:
  1. Overlap - LLM and TTS run at the same time (measured: ~294 ms)
  2. Not finishing work you do not need yet - TTS emits its first chunk instead
     of synthesizing the whole reply first (measured: ~950 ms)

The second is the larger win, and it is the one people forget.
"""

import asyncio
import sys
import time
from pathlib import Path

from loguru import logger

from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.services.kokoro.tts import KokoroTTSService
from pipecat.services.ollama.llm import OLLamaLLMService, OllamaLLMSettings
from pipecat.services.whisper.stt import MLXModel, WhisperSTTServiceMLX
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from tracing_setup import (
    TurnEndObserver,
    emit_e2e_span,
    emit_turn_detection_span,
    init_tracing,
)
from wav_transport import Capture, WavFileTransport

logger.remove()
logger.add(sys.stderr, level="INFO")

WAV = "fixtures/02-medium.wav"
REPS = 3

# THE KNOB THIS TALK IS ABOUT.  (default lives in config.py)
#
# Pipecat's default is 0.2s. Measured internal silences in the fixtures:
# 01-short none, 02-medium 260ms, 03-trailing-pause 420ms. So at 0.2s the VAD
# ends the turn on a clause pause, the agent answers a half-sentence, and the
# work is thrown away. Raising this fixes it by WAITING LONGER. That wait is
# not compute. It is the agent deliberately doing nothing, and on a naive
# implementation it is the single largest line in the latency budget.
from config import CONFIG
VAD_STOP_SECS = CONFIG.vad_stop_secs
TRACES = Path("artifacts/spike-traces.jsonl")
async def one_run(rep: int, wav: str = None, stop_secs: float = None) -> Capture:
    wav = wav or WAV
    stop_secs = VAD_STOP_SECS if stop_secs is None else stop_secs
    capture = Capture()
    params = TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        audio_in_sample_rate=CONFIG.input_sample_rate,
        audio_out_sample_rate=CONFIG.output_sample_rate,
    )
    transport = WavFileTransport(params, wav, capture,
                                 trailing_silence_s=CONFIG.trailing_silence_s)

    stt = WhisperSTTServiceMLX(
        model=CONFIG.stt_model,
        # Without this Pipecat assumes 1.0 s for a local model that returns in
        # tens of milliseconds, and holds the turn open waiting. See config.py.
        ttfs_p99_latency=CONFIG.stt_ttfs_p99,
    )
    # EVERY value here comes from CONFIG. Hardcoding any of them is how the two
    # builds silently end up running different models, which invalidates the
    # whole comparison. That is not hypothetical: this file hardcoded
    # llama3.2:3b while naive.py read CONFIG, and one tuned run compared a 1b
    # naive build against a 3b streaming build before it was caught.
    llm = OLLamaLLMService(
        model=CONFIG.llm_model,
        settings=OllamaLLMSettings(
            system_instruction=CONFIG.system_prompt,
            temperature=CONFIG.llm_temperature,
            seed=CONFIG.llm_seed,
            max_tokens=CONFIG.llm_max_tokens,
        ),
    )
    tts = KokoroTTSService(voice_id=CONFIG.tts_voice)

    context = LLMContext()
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=stop_secs)),
            # Endpointing is stated explicitly rather than left to the
            # default. Pipecat's default strategy adds a 0.6 s policy window on
            # top of the VAD silence window (see CONFIG.user_speech_timeout),
            # which would make this build wait ~600 ms longer than naive.py for
            # reasons that have nothing to do with scheduling.
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
            # NOT using filter_incomplete_user_turns: measured, it costs an
            # 860-token classifier call plus a ~5s wait for speech that never
            # arrives. 2253ms -> 7831ms. It treats the symptom anyway; the
            # cause is VAD ending speech on a clause pause. See VAD_STOP_SECS.
        ),
    )

    pipeline = Pipeline([
        transport.input(),
        stt,
        user_agg,
        llm,
        tts,
        transport.output(),
        assistant_agg,
    ])

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[TurnEndObserver(capture)],
        enable_tracing=True,
        enable_turn_tracking=True,
        conversation_id=f"spike-{rep}",
        additional_span_attributes={"mode": "streaming", "fixture": Path(wav).stem, "rep": rep,
                                   "vad.stop_secs": stop_secs},
    )

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

    emit_turn_detection_span(capture, "vad_timeout", stop_secs, measured_as="pipeline_frame_observed")
    emit_e2e_span(capture, "streaming", Path(wav).stem)
    return capture


async def main():
    TRACES.unlink(missing_ok=True)
    ok, _ = init_tracing("voice-infer-spike", TRACES)
    logger.info(f"tracing initialized: {ok}")

    results = []
    for rep in range(REPS):
        logger.info(f"--- rep {rep} {'(WARMUP, discarded)' if rep == 0 else ''} ---")
        results.append(await one_run(rep))

    from opentelemetry import trace
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()

    print("\n" + "=" * 62)
    print(f"SPIKE: {WAV}   ({REPS} reps, rep 0 = warmup)")
    print("=" * 62)
    print(f"  {'rep':<6}{'e2e_ms':>10}{'chunks':>9}{'gap_p50':>10}{'drift_ms':>10}")
    for i, c in enumerate(results):
        e2e = c.e2e_speech_end_to_first_audio
        st = c.cadence_stats()
        tag = " (warmup)" if i == 0 else ""
        print(f"  {i:<6}{(e2e * 1000 if e2e else -1):>10.0f}{st.get('chunks',0):>9}"
              f"{st.get('gap_ms_p50',0):>10.2f}{st.get('cumulative_drift_ms',0):>10.1f}{tag}")
    warm = [c.e2e_speech_end_to_first_audio for c in results[1:]
            if c.e2e_speech_end_to_first_audio]
    if warm:
        print(f"\n  WARM median e2e: {sorted(warm)[len(warm)//2] * 1000:.0f} ms")
    print("=" * 62)
    return 0 if warm else 1


if __name__ == "__main__":
    if len(sys.argv) > 1:
        WAV = sys.argv[1]
    if len(sys.argv) > 2:
        REPS = int(sys.argv[2])
    if len(sys.argv) > 3:
        VAD_STOP_SECS = float(sys.argv[3])
    sys.exit(asyncio.run(main()))
