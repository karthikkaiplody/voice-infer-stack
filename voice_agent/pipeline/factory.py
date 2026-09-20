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

import hashlib
import shutil
from pathlib import Path

from voice_agent import paths
from voice_agent.knowledge import agents
from voice_agent.config import CONFIG

# Where Piper voices live. Pipecat would otherwise download into whatever the
# working directory happens to be. Inside the repo, and git-ignored (`models/`),
# so a clone is self-contained and `make setup-demo` pre-fetches it.
PIPER_DIR = paths.PIPER_DIR

# espeak-ng (used by both Kokoro and Piper to turn text into phonemes) copies
# its data path into a fixed 160-byte buffer. Past 159 characters it does not
# report a clear error: it falls back to a path baked in at build time, fails to
# find `phontab` there, and EXITS THE WHOLE PROCESS. A checkout in a deeply
# nested folder (a long workspace name is enough) puts the data 170+ characters
# deep inside `.venv`, so speech would kill the server on its first sentence.
ESPEAK_MAX_PATH = 159
ESPEAK_LINK_ROOT = Path.home() / ".cache" / "voice-infer-stack"


def espeak_safe_data_dir(real: Path) -> Path:
    """A path to espeak-ng's data that is short enough for espeak-ng to open.

    The real directory when it already fits. Otherwise a one-time copy (about
    19 MB) under the user's cache. A symlink would be cheaper but does not work:
    phonemizer resolves the path it is given, which turns a short link straight
    back into the long real path. Nothing is installed.
    """
    real = Path(real)
    if len(str(real)) <= ESPEAK_MAX_PATH:
        return real
    key = hashlib.sha1(str(real).encode()).hexdigest()[:8]
    copy = ESPEAK_LINK_ROOT / key / "espeak-ng-data"
    if len(str(copy)) > ESPEAK_MAX_PATH:
        raise RuntimeError(
            "This checkout is in a folder too deeply nested for the speech "
            "engine (espeak-ng), and the cache folder is too. Clone the repo "
            "somewhere with a shorter path, for example ~/voice-infer-stack.")

    def complete() -> bool:
        marker, source = copy / "phontab", real / "phontab"
        return (not copy.is_symlink() and marker.is_file()
                and marker.stat().st_size == source.stat().st_size)

    if not complete():
        staging = copy.parent / "partial"
        shutil.rmtree(copy.parent, ignore_errors=True)
        copy.parent.mkdir(parents=True)
        shutil.copytree(real, staging)
        staging.rename(copy)        # only ever visible whole
    return copy


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


def component_identity(config=CONFIG) -> dict[str, str]:
    """Configured engines and the model/voice each engine will actually use."""
    _check("VOICE_STT_ENGINE", config.stt_engine, STT_ENGINES)
    _check("VOICE_TTS_ENGINE", config.tts_engine, TTS_ENGINES)
    return {
        "stt_engine": config.stt_engine,
        "stt_model": (config.stt_model if config.stt_engine == "mlx"
                      else config.stt_model_faster_whisper),
        "llm_provider": "ollama",
        "llm_model": config.llm_model,
        "tts_engine": config.tts_engine,
        "tts_voice": (config.tts_voice if config.tts_engine == "kokoro"
                      else config.tts_voice_piper),
    }


def endpointing_strategy(config=CONFIG) -> str:
    """The end-of-turn policy `agent.build_pipeline` actually installs.

    Mirrors the branch there (`use_smart_turn` picks the turn analyzer, otherwise
    the speech-timeout policy runs on top of VAD), so telemetry names the policy
    that decided rather than a constant.
    """
    return "smart_turn" if config.use_smart_turn else "vad_timeout"


def _check(name, value, allowed):
    if value not in allowed:
        raise SystemExit(
            f"unknown {name}: {value!r}. Choose one of {', '.join(allowed)}."
        )


def make_stt(config=CONFIG):
    """Speech to text. Returns a Pipecat service."""
    _check("VOICE_STT_ENGINE", config.stt_engine, STT_ENGINES)
    if config.stt_engine == "mlx":
        from pipecat.services.whisper.stt import WhisperSTTServiceMLX

        return WhisperSTTServiceMLX(
            model=config.stt_model,
            # Pipecat otherwise assumes 1.0 s for a model that returns in tens
            # of milliseconds and holds the turn open waiting. See config.py.
            ttfs_p99_latency=config.stt_ttfs_p99,
        )

    from pipecat.services.whisper.stt import WhisperSTTService

    return WhisperSTTService(
        model=config.stt_model_faster_whisper,
        ttfs_p99_latency=config.stt_ttfs_p99,
    )


def make_llm(config=CONFIG):
    """The language model. Any model Ollama serves."""
    from pipecat.services.ollama.llm import OLLamaLLMService, OllamaLLMSettings

    return OLLamaLLMService(
        model=config.llm_model,
        settings=OllamaLLMSettings(
            system_instruction=agents.system_prompt_for(config),
            temperature=config.llm_temperature,
            seed=config.llm_seed,
            max_tokens=config.llm_max_tokens,
        ),
    )


def make_tts(config=CONFIG):
    """Text to speech. Returns a Pipecat service."""
    _check("VOICE_TTS_ENGINE", config.tts_engine, TTS_ENGINES)
    if config.tts_engine == "kokoro":
        import espeakng_loader
        from pipecat.services.kokoro.tts import KokoroTTSService

        # kokoro-onnx asks espeakng_loader for the data path when it builds its
        # tokenizer, so a safe path has to be what it is told.
        safe = str(espeak_safe_data_dir(Path(espeakng_loader.get_data_path())))
        espeakng_loader.get_data_path = lambda: safe
        return KokoroTTSService(voice_id=config.tts_voice)

    from piper.phonemize_espeak import ESPEAK_DATA_DIR
    from pipecat.services.piper.tts import PiperTTSService

    service = PiperTTSService(voice_id=config.tts_voice_piper,
                              download_dir=PIPER_DIR)
    # The voice builds its phonemizer lazily, from this field, on first speech.
    service._voice.espeak_data_dir = espeak_safe_data_dir(ESPEAK_DATA_DIR)
    return service


def make_vad(config=CONFIG):
    """Voice activity detection. Silero is the only local option Pipecat ships."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams

    return SileroVADAnalyzer(params=VADParams(
        stop_secs=config.vad_stop_secs,
        min_volume=config.vad_min_volume,
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


def describe(config=CONFIG) -> str:
    """One line naming every component, for the run log and the trace."""
    selected = component_identity(config)
    endpoint = endpointing_strategy(config)
    return (f"stt={selected['stt_engine']}:{selected['stt_model']}  "
            f"llm={selected['llm_provider']}:{selected['llm_model']}  "
            f"tts={selected['tts_engine']}:{selected['tts_voice']}  vad=silero  "
            f"endpoint={endpoint}@{config.vad_stop_secs}s")
