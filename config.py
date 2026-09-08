"""Single source of truth for both execution modes.

Both naive.py and streaming.py import this. If the two modes ever disagree on
model, sample rate, prompt, or decoding parameters, the comparison between them
is confounded and every chart built on it is wrong. One frozen object, imported
twice, is the cheapest defence against that.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # --- models ---
    stt_model: str = "mlx-community/whisper-large-v3-turbo-q4"
    llm_model: str = "llama3.2:3b"
    tts_voice: str = "af_heart"

    # --- audio ---
    input_sample_rate: int = 16000   # what whisper, silero and smart-turn expect
    output_sample_rate: int = 24000  # kokoro native
    chunk_ms: int = 20               # one virtual-microphone frame

    # --- turn detection ---
    # Pipecat's default is 0.2s. Measured fixture pauses exceed that, so the
    # default splits an utterance mid-sentence. See sweep.py.
    vad_stop_secs: float = 0.5
    # How much generated silence follows the utterance. The detector needs
    # silence to decide the turn is over; without it nothing ever fires.
    trailing_silence_s: float = 3.0
    # Both builds use Silero VAD alone so that SCHEDULING is the only thing
    # that differs between them. Enabling smart turn on one side only would
    # change the detector AND the scheduling at once, and the comparison would
    # no longer mean anything. Smart turn is a separate lever, measured on its
    # own in sweep.py.
    use_smart_turn: bool = False

    # Pipecat's endpointing knobs, stated explicitly rather than left implicit.
    # Both are at Pipecat's own defaults: changing them was measured and did NOT
    # explain the gap below, so running at stock is the more representative
    # choice.
    #   user_speech_timeout - a policy window after VAD stop in which the user
    #     may resume before the turn is closed.
    #   wait_for_transcript - also wait for STT to return a transcript.
    user_speech_timeout: float = 0.6
    wait_for_transcript: bool = True

    # THE 600 MS. Pipecat's stop strategy runs two timers in parallel and closes
    # the turn only when both finish: user_speech_timeout above, and a safety
    # net sized to the STT service's P99 speech-end-to-final-transcript latency.
    #
    # SegmentedSTTService does not report that latency, so Pipecat falls back to
    # a default of 1.0 s and logs "ttfs_p99_latency not set, using default 1.0s".
    # That default is sized for a network round trip to a hosted STT. Whisper
    # tiny returns in 67 ms locally, so the pipeline was waiting roughly half a
    # second for a transcript that had already arrived.
    #
    # Measured by sweeping user_speech_timeout: 0.0 -> 1093 ms, 0.6 -> 1162 ms,
    # 1.2 -> 1766 ms. Below ~1.1 s the setting made almost no difference,
    # because this other timer was the binding constraint.
    #
    # Set it to something honest for a local model and the wait disappears.
    stt_ttfs_p99: float = 0.15

    # RESOLVED. Was a ~600 ms gap between the builds' turn_detection rows,
    # caused by the two safety-net timers above, both sized for hosted services:
    # a 1.0 s STT default and a 0.6 s resume window. With both set honestly for
    # a local stack the rows agree to within ~50 ms (322 ms naive, 373 ms
    # streaming), the residual being the pipeline transit of the frame.
    # Historical note kept because the investigation is the interesting part:
    # naive.py observes end-of-turn as a Silero VAD state transition and
    # measures ~562 ms after end of speech, consistent with stop_secs=0.5.
    # streaming.py observes UserStoppedSpeakingFrame travelling the pipeline and
    # measures ~1065-1158 ms. Setting user_speech_timeout to 0 and
    # wait_for_transcript to False were both tried and neither accounts for the
    # difference, so the cause is still unattributed.
    # End-to-end totals are unaffected: those come from one clock on both sides.
    # budget.py marks the row rather than implying the two are the same number.

    # --- determinism ---
    # A varying reply length changes TTS duration, which changes end-to-end
    # latency. Pinning these keeps runs comparable across modes and reps.
    llm_temperature: float = 0.0
    llm_seed: int = 42
    llm_max_tokens: int = 60
    system_prompt: str = (
        "You are a voice assistant. Answer in two or three short spoken "
        "sentences. No emojis, no lists, no formatting."
    )

    # --- benchmark ---
    warmup_reps: int = 1
    measured_reps: int = 4


def _from_env() -> Config:
    """Allow every knob to be overridden from the environment.

    This exists so a "can it fit in 800 ms?" run is a set of environment
    variables rather than an edited file. Whatever is set here applies to BOTH
    builds, so tuning never quietly turns the naive/streaming comparison into a
    comparison of different models.

        VOICE_STT_MODEL=mlx-community/whisper-tiny \
        VOICE_LLM_MODEL=llama3.2:1b \
        VOICE_VAD_STOP_SECS=0.3 \
        uv run python bench.py --fixture 01-short
    """
    import os
    from dataclasses import fields, replace

    base = Config()
    changes = {}
    for f in fields(Config):
        env = os.environ.get(f"VOICE_{f.name.upper()}")
        if env is None:
            continue
        if f.type is bool or isinstance(getattr(base, f.name), bool):
            changes[f.name] = env.lower() in ("1", "true", "yes")
        elif isinstance(getattr(base, f.name), int):
            changes[f.name] = int(env)
        elif isinstance(getattr(base, f.name), float):
            changes[f.name] = float(env)
        else:
            changes[f.name] = env
    return replace(base, **changes) if changes else base


CONFIG = _from_env()
