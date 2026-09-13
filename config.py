"""Every knob in the repo, in one frozen object.

Nothing downstream hardcodes a model, a sample rate, a prompt or a threshold:
it reads them from here, and every field can be overridden with a `VOICE_*`
environment variable. That is what makes the repo a playground rather than a
fixed demo: changing a component is one variable on one command line, not an
edit.

A value hardcoded in a pipeline file has already broken this repo once, in a
way nothing noticed for two commits.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    # --- models ---
    # --- which engine runs each stage (see factory.py) ---
    stt_engine: str = "mlx"          # mlx | faster-whisper
    tts_engine: str = "kokoro"       # kokoro | piper

    stt_model: str = "mlx-community/whisper-large-v3-turbo-q4"
    # faster-whisper names its models differently from MLX, so each engine
    # carries its own, and swapping engines does not silently ask one of them
    # for a model identifier it has never heard of.
    stt_model_faster_whisper: str = "base"
    tts_voice_piper: str = "en_US-ryan-high"
    llm_model: str = "llama3.2:3b"
    tts_voice: str = "af_heart"

    # --- audio ---
    input_sample_rate: int = 16000   # what whisper, silero and smart-turn expect
    output_sample_rate: int = 24000  # kokoro native
    chunk_ms: int = 20               # one virtual-microphone frame

    # --- turn detection ---
    # Pipecat's default is 0.2s. Measured fixture pauses exceed that, so the
    # default splits an utterance mid-sentence: the agent answers half a
    # question, throws the work away, and answers again.
    vad_stop_secs: float = 0.5
    # Pipecat's default is 0.6, which assumes a hot signal. Laptop and USB mics
    # vary enormously, and a gate the microphone never reaches looks exactly
    # like a broken pipeline: the page sits at "Listening" forever. The live UI
    # shows an input meter so this is visible rather than guessed at.
    vad_min_volume: float = 0.3
    # Which input device. None means the system default. `make devices` lists
    # them; on a machine with virtual audio (Elgato, Loopback) the default is
    # often not the microphone you are actually talking into.
    audio_device: int | None = None
    # How much generated silence follows the utterance. The detector needs
    # silence to decide the turn is over; without it nothing ever fires.
    trailing_silence_s: float = 3.0
    # Silero VAD alone by default: a silence timer, which is the mechanism
    # worth understanding first because it is the one with a number you can
    # move. Set this to swap in the smart-turn model, which decides from the
    # audio whether you sound finished, and watch the row change shape.
    use_smart_turn: bool = False

    # Pipecat's endpointing knobs, stated explicitly rather than left implicit.
    # Both sit at Pipecat's own defaults. `make live` overrides them for a
    # local stack, so you can hear the difference the two timers make.
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
    # That default is not a network allowance: hosted services ship measured
    # values, and 1.0 s is the conservative catch-all for local models, whose
    # speed depends entirely on the hardware in front of them. The net is
    # short-circuited the moment STT flags a transcript as final, so the full
    # second is the worst case -- but Whisper tiny returns in 67 ms locally,
    # and the worst case is what an unflagged transcript gets you: the pipeline
    # waiting roughly half a second for something that had already arrived.
    #
    # Measured by sweeping user_speech_timeout: 0.0 -> 1093 ms, 0.6 -> 1162 ms,
    # 1.2 -> 1766 ms. Below ~1.1 s the setting made almost no difference,
    # because this other timer was the binding constraint.
    #
    # Set it to something honest for a local model and the wait disappears.
    stt_ttfs_p99: float = 0.15

    # Worth knowing when you read the turn_detection row: with the two timers
    # above at their stock values the wait measured ~1.1 s after the end of
    # speech, against a 0.5 s silence window. Set honestly for a local stack it
    # drops to ~370 ms. Neither number is a model cost, and no model in the
    # stack was changed to move it.

    # --- determinism ---
    # A varying reply length changes TTS duration, which changes end-to-end
    # latency. Pinning these keeps runs comparable across modes and reps.
    llm_temperature: float = 0.0
    llm_seed: int = 42
    llm_max_tokens: int = 60
    # A scenario, not a general assistant. The demo is a library information
    # line: it gives the pipeline a bounded job, makes replies short and
    # predictable, and keeps reply length stable across runs, which matters
    # because reply length drives the text-to-speech stage.
    system_prompt: str = (
        "You are the automated information line for Riverside Public Library. "
        "You answer ONLY questions about this library: opening hours, borrowing "
        "and returns, renewals, fines, library cards, rooms, events, and how to "
        "find a book. "
        "If asked anything else, say one short sentence: you can only help with "
        "library questions. Do not answer it. "
        "Facts you know: open 9am to 8pm Monday to Friday, 10am to 6pm Saturday, "
        "12pm to 5pm Sunday. Loans last three weeks and renew twice online. "
        "Fines are 10 cents a day, capped at 5 dollars. A library card is free "
        "with proof of address. Study rooms are bookable two weeks ahead. "
        "Answer in one or two short spoken sentences. Never use lists, emojis or "
        "formatting. You are being read aloud."
    )

    # --- repeat runs ---
    # The first inference pays model load and graph compilation and is several
    # times slower than the rest, so anything that runs the pipeline more than
    # once says which run was cold rather than averaging it in.
    warmup_reps: int = 1
    measured_reps: int = 4


def _from_env() -> Config:
    """Allow every knob to be overridden from the environment.

    This exists so "what happens if the model is smaller?" is a variable on a
    command line rather than an edited file, and so the answer arrives as a
    changed shape on the live page.

        VOICE_STT_MODEL=mlx-community/whisper-tiny \
        VOICE_LLM_MODEL=llama3.2:1b \
        VOICE_VAD_STOP_SECS=0.3 \
        make live
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
