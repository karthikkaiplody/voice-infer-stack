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
    trailing_silence_s: float = 3.0

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
