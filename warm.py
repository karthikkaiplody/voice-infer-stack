"""Pre-download every model weight the benchmark needs.

Run this once before benchmarking. Everything here is a network fetch into a
local cache; nothing is measured. Kept separate from bench.py precisely so a
cold download can never land inside a timed run.
"""

import sys
import time


def step(name, fn):
    t = time.monotonic()
    try:
        fn()
        print(f"OK   {name}  ({time.monotonic() - t:.1f}s)", flush=True)
        return True
    except Exception as e:
        print(f"FAIL {name}: {type(e).__name__}: {e}", flush=True)
        return False


def kokoro():
    from pathlib import Path
    from pipecat.services.kokoro.tts import (
        KOKORO_CACHE_DIR,
        _ensure_model_files,
    )
    KOKORO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_model_files(
        Path(KOKORO_CACHE_DIR) / "kokoro-v1.0.onnx",
        Path(KOKORO_CACHE_DIR) / "voices-v1.0.bin",
    )


def mlx_whisper():
    from huggingface_hub import snapshot_download
    from pipecat.services.whisper.stt import MLXModel
    snapshot_download(MLXModel.LARGE_V3_TURBO_Q4.value)


def smart_turn():
    from transformers import Wav2Vec2Processor
    from pipecat.audio.turn.smart_turn.local_smart_turn_v2 import (
        _Wav2Vec2ForEndpointing,
    )
    _Wav2Vec2ForEndpointing.from_pretrained("pipecat-ai/smart-turn-v2")
    Wav2Vec2Processor.from_pretrained("pipecat-ai/smart-turn-v2")


def silero():
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    SileroVADAnalyzer()


results = [
    step("kokoro-onnx", kokoro),
    step("mlx-whisper large-v3-turbo-q4", mlx_whisper),
    step("smart-turn-v2", smart_turn),
    step("silero-vad", silero),
]
print(f"\n{sum(results)}/{len(results)} ready")
sys.exit(0 if all(results) else 1)
