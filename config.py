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

    # KNOWN GAP, do not present these two rows side by side.
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


CONFIG = Config()
