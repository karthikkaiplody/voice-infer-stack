"""Build the four stages from config, so components are genuinely swappable.

This is the seam that makes the repo a playground. Every way of running the
pipeline constructs its stages here, so a swap is one environment variable and
it applies everywhere at once -- including to the live page, where the change
shows up as a differently shaped waterfall.

    VOICE_STT_ENGINE=faster-whisper  make live
    VOICE_TTS_ENGINE=piper           make live
    VOICE_LLM_MODEL=qwen2.5:0.5b     make live

Every engine here runs locally and needs no API key. Adding one means adding a
branch below and a row in the table in README.md.
"""

from __future__ import annotations

from config import CONFIG

# --- what exists, and honestly what does not ------------------------------
#
# STT   mlx            WhisperSTTServiceMLX. Apple Silicon only, fastest here.
#       faster-whisper WhisperSTTService. Runs anywhere, CPU or CUDA.
#       Both are SEGMENTED: neither emits a partial transcript while you speak.
#       That is a property of the model, not of the engine, and no engine swap
#       in this file changes it.
#
# TTS   kokoro         Kokoro-82M via ONNX Runtime. Downloads on first use.
#       piper          Piper voices. Downloads on first use.
#
# LLM   ollama         Any model Ollama serves. Swap the MODEL, not the engine.
#
# VAD   silero         The only local VAD Pipecat ships. There is no second
#                      option to offer, so this file does not pretend there is.
#
# Endpointing  vad_timeout  wait for stop_secs of silence
#              smart_turn   an ONNX model decides. CONFIG.use_smart_turn.

STT_ENGINES = ("mlx", "faster-whisper")
TTS_ENGINES = ("kokoro", "piper")


def _check(name, value, allowed):
    if value not in allowed:
        raise SystemExit(
            f"unknown {name}: {value!r}. Choose one of {', '.join(allowed)}."
        )


def make_stt():
    """Speech to text. Returns a Pipecat service."""
    _check("VOICE_STT_ENGINE", CONFIG.stt_engine, STT_ENGINES)
    if CONFIG.stt_engine == "mlx":
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        return WhisperSTTServiceMLX(
            model=CONFIG.stt_model,
            # Pipecat otherwise assumes 1.0 s for a model that returns in tens
            # of milliseconds and holds the turn open waiting. See config.py.
            ttfs_p99_latency=CONFIG.stt_ttfs_p99,
        )

    from pipecat.services.whisper.stt import WhisperSTTService

    return WhisperSTTService(
        model=CONFIG.stt_model_faster_whisper,
        ttfs_p99_latency=CONFIG.stt_ttfs_p99,
    )


def make_llm():
    """The language model. Any model Ollama serves."""
    from pipecat.services.ollama.llm import OLLamaLLMService, OllamaLLMSettings

    return OLLamaLLMService(
        model=CONFIG.llm_model,
        settings=OllamaLLMSettings(
            system_instruction=CONFIG.system_prompt,
            temperature=CONFIG.llm_temperature,
            seed=CONFIG.llm_seed,
            max_tokens=CONFIG.llm_max_tokens,
        ),
    )


def make_tts():
    """Text to speech. Returns a Pipecat service."""
    _check("VOICE_TTS_ENGINE", CONFIG.tts_engine, TTS_ENGINES)
    if CONFIG.tts_engine == "kokoro":
        from pipecat.services.kokoro.tts import KokoroTTSService

        return KokoroTTSService(voice_id=CONFIG.tts_voice)

    from pipecat.services.piper.tts import PiperTTSService

    return PiperTTSService(voice_id=CONFIG.tts_voice_piper)


def make_vad():
    """Voice activity detection. Silero is the only local option Pipecat ships."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    return SileroVADAnalyzer(params=VADParams(
        stop_secs=CONFIG.vad_stop_secs,
        min_volume=CONFIG.vad_min_volume,
    ))


def list_input_devices():
    """Every input device, so a silent pipeline can be diagnosed rather than guessed at."""
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        default = pa.get_default_input_device_info()["index"]
        out = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0:
                out.append({"index": d["index"], "name": d["name"],
                            "channels": d["maxInputChannels"],
                            "default": d["index"] == default})
        return out
    finally:
        pa.terminate()


def describe() -> str:
    """One line naming every component, for the run log and the trace."""
    stt = (CONFIG.stt_model if CONFIG.stt_engine == "mlx"
           else CONFIG.stt_model_faster_whisper)
    tts = (CONFIG.tts_voice if CONFIG.tts_engine == "kokoro"
           else CONFIG.tts_voice_piper)
    endpoint = "smart_turn" if CONFIG.use_smart_turn else "vad_timeout"
    return (f"stt={CONFIG.stt_engine}:{stt}  llm=ollama:{CONFIG.llm_model}  "
            f"tts={CONFIG.tts_engine}:{tts}  vad=silero  "
            f"endpoint={endpoint}@{CONFIG.vad_stop_secs}s")
