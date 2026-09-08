"""BUILD 1 - the sequential voice agent.

This is the version almost everybody writes first, and there is nothing wrong
with it. Each stage waits for the one before it to finish completely:

    listen ──► transcribe ──► generate ──► speak
       │            │             │           │
    wait for     whole        whole        whole
    silence    utterance      reply        reply

Same models, same config, same audio as streaming.py. The ONLY difference is
that nothing here overlaps. That is the entire point of the comparison: if the
two builds differ in models or input, the comparison means nothing.

No framework is used, deliberately. A pipeline framework's job is to overlap
work, so demonstrating what happens WITHOUT overlap is clearer in plain
async Python that you can read top to bottom.
"""

import asyncio
import time
import wave
from pathlib import Path

import numpy as np
from loguru import logger

import analysis
from config import CONFIG
from tracing_setup import (
    emit_e2e_span,
    emit_turn_detection_span,
    stage_span,
)
from wav_transport import CHUNK_MS, Capture


async def listen(wav_path: str, capture: Capture) -> tuple[bytes, float]:
    """Play the WAV in at real time until the detector says the turn is over.

    Real time matters. You cannot transcribe audio that has not arrived yet, so
    consuming the file instantly would invent latency that no microphone could
    deliver. The detector is the same Silero VAD with the same stop_secs the
    streaming build uses, driven by hand instead of by a pipeline.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams, VADState

    vad = SileroVADAnalyzer(params=VADParams(stop_secs=CONFIG.vad_stop_secs))
    vad.set_sample_rate(CONFIG.input_sample_rate)

    with wave.open(wav_path, "rb") as wf:
        pcm = wf.readframes(wf.getnframes())

    chunk = int(CONFIG.input_sample_rate * CHUNK_MS / 1000) * 2
    silence = b"\x00" * chunk
    frames = [pcm[i:i + chunk] for i in range(0, len(pcm), chunk)]
    n_speech = len(frames)
    frames += [silence] * int(CONFIG.trailing_silence_s * 1000 / CHUNK_MS)

    # Ground truth for "the user stopped talking", measured from the audio by
    # make_fixtures.py. Falling back to the last frame would use the end of the
    # FILE, which includes trailing silence and understates every number.
    speech_end_ms = analysis.speech_end_ms(wav_path)

    period = CHUNK_MS / 1000
    capture.t_origin = time.monotonic()
    capture.wall_at_origin_ns = time.time_ns()
    deadline = capture.t_origin
    if speech_end_ms is not None:
        capture.t_speech_end = capture.t_origin + speech_end_ms / 1000
    spoke = False

    for i, frame in enumerate(frames):
        now = time.monotonic()
        if now < deadline:
            await asyncio.sleep(deadline - now)
        emitted = time.monotonic()
        capture.chunk_arrivals.append(emitted)
        if speech_end_ms is None and i == n_speech - 1:
            capture.t_speech_end = emitted + period
        deadline += period

        if len(frame) < chunk:
            frame = frame + b"\x00" * (chunk - len(frame))
        state = await vad.analyze_audio(frame)
        if state == VADState.SPEAKING:
            spoke = True
        elif spoke and state == VADState.QUIET:
            # The turn is over. Note what this cost: the detector spent no
            # meaningful compute. It simply waited for silence.
            capture.t_turn_end = time.monotonic()
            logger.info(
                f"turn ended, waited {capture.turn_detection_wait * 1000:.0f} ms"
                f" after speech"
            )
            break

    emit_turn_detection_span(capture, "vad_timeout", CONFIG.vad_stop_secs, measured_as="vad_state_transition")
    return pcm[:n_speech * chunk], capture.rel(time.monotonic())


def transcribe(pcm: bytes) -> str:
    """Whole utterance in, whole transcript out. Nothing partial."""
    import mlx_whisper

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    with stage_span(
        "stt",
        **{
            "gen_ai.provider.name": "whisper-mlx",
            "gen_ai.request.model": CONFIG.stt_model,
            "is_final": True,
        },
    ) as span:
        result = mlx_whisper.transcribe(audio, path_or_hf_repo=CONFIG.stt_model)
        text = result["text"].strip()
        span.set_attribute("transcript", text)
    logger.info(f"transcript: {text!r}")
    return text


def generate(transcript: str) -> str:
    """Whole reply, generated to completion before a single word is spoken.

    `stream=False` is the whole story of this build. The tokens exist for
    hundreds of milliseconds before anything is done with them.
    """
    from openai import OpenAI

    client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
    with stage_span(
        "llm",
        **{
            "gen_ai.provider.name": "ollama",
            "gen_ai.request.model": CONFIG.llm_model,
            "stream": False,
        },
    ) as span:
        response = client.chat.completions.create(
            model=CONFIG.llm_model,
            messages=[
                {"role": "system", "content": CONFIG.system_prompt},
                {"role": "user", "content": transcript},
            ],
            temperature=CONFIG.llm_temperature,
            seed=CONFIG.llm_seed,
            max_tokens=CONFIG.llm_max_tokens,
            stream=False,
        )
        reply = (response.choices[0].message.content or "").strip()
        usage = response.usage
        if usage:
            span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
        span.set_attribute("output", reply)
    logger.info(f"reply: {reply!r}")
    return reply


def synthesize(reply: str, capture: Capture) -> np.ndarray:
    """Whole reply synthesized before any of it is played."""
    from kokoro_onnx import Kokoro
    from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, _ensure_model_files

    model = Path(KOKORO_CACHE_DIR) / "kokoro-v1.0.onnx"
    voices = Path(KOKORO_CACHE_DIR) / "voices-v1.0.bin"
    _ensure_model_files(model, voices)

    with stage_span(
        "tts",
        **{
            "gen_ai.provider.name": "kokoro",
            "voice_id": CONFIG.tts_voice,
            "metrics.character_count": len(reply),
        },
    ) as span:
        kokoro = Kokoro(str(model), str(voices))
        samples, rate = kokoro.create(reply, voice=CONFIG.tts_voice, speed=1.0,
                                      lang="en-us")
        # First audio exists only now, once the ENTIRE reply is synthesized.
        capture.t_first_audio = time.monotonic()
        span.set_attribute("metrics.ttfb", capture.t_first_audio - capture.t_origin)
    return np.asarray(samples, dtype=np.float32), rate


async def run(wav_path: str) -> Capture:
    capture = Capture()
    pcm, _ = await listen(wav_path, capture)
    transcript = transcribe(pcm)
    reply = generate(transcript)
    samples, rate = synthesize(reply, capture)
    capture.out_chunks.append((capture.t_first_audio, samples.tobytes()))
    capture.tts_rate = rate
    capture.reply_audio = samples
    emit_e2e_span(capture, "naive", Path(wav_path).stem)
    e2e = capture.e2e_speech_end_to_first_audio
    logger.info(f"BUILD 1 (sequential) end to end: {e2e * 1000:.0f} ms")
    return capture


if __name__ == "__main__":
    import sys

    from tracing_setup import init_tracing

    wav = sys.argv[1] if len(sys.argv) > 1 else "fixtures/02-medium.wav"
    traces = Path("artifacts/naive-traces.jsonl")
    traces.unlink(missing_ok=True)
    init_tracing("voice-infer-naive", traces)

    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    for rep in range(reps):
        logger.info(f"--- rep {rep}{' (WARMUP)' if rep == 0 else ''} ---")
        asyncio.run(run(wav))

    from opentelemetry import trace as _t
    p = _t.get_tracer_provider()
    if hasattr(p, "force_flush"):
        p.force_flush()
