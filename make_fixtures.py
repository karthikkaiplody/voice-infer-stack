"""Generate the repo's input utterances locally with Kokoro.

No API keys, no account, no committed binaries needed to get started: `uv run
python make_fixtures.py` reproduces every input WAV from text.

CAVEAT: synthetic speech has clean endpoints. No breath, no trailing "umm", no
hesitation. That makes a turn detector look BETTER than it does on human
speech, so a turn-detection number from these fixtures is a best case. Talk to
it yourself, or `make record`, and the same row gets longer. Human-recorded
fixtures live alongside these and are labelled as such in manifest.json.
"""

import hashlib
import json
import wave
from pathlib import Path

import numpy as np
from kokoro_onnx import Kokoro

from pipecat.services.kokoro.tts import KOKORO_CACHE_DIR, _ensure_model_files

FIXTURES = Path("fixtures")
TARGET_SR = 16000  # what Whisper, Silero VAD, and smart-turn all expect
VOICE = "af_heart"

# Kept deliberately small. Three fixtures cover the talk: a short question, a
# normal one, and one with a mid-thought pause that turn detection can trip on.
UTTERANCES = [
    ("01-short", "What time is it?"),
    ("02-medium", "Can you tell me what the weather is going to be like tomorrow afternoon?"),
    ("03-trailing-pause", "I was wondering if you could help me... find a good restaurant nearby."),
]


def analyse_speech(pcm: bytes, sr: int, frame_ms: int = 20, thresh: float = 0.02):
    """Measure where speech actually ends, and every internal pause.

    Two reasons this is measured rather than assumed:

    1. `t_speech_end_ms` is t0 for end-to-end latency. Taking it as the file
       duration is only correct if the generator trims trailing silence, and it
       makes the plan's invariant (t_speech_end < duration) impossible to hold.
    2. Internal pause length is what determines the minimum workable VAD
       `stop_secs`. A pause longer than stop_secs ends the turn mid-sentence.
       Publishing the pause lengths is what makes a turn-detection result interpretable
       instead of a table of unexplained thresholds.
    """
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    win = int(sr * frame_ms / 1000)
    n = len(a) // win
    rms = np.array([np.sqrt(np.mean(a[i * win:(i + 1) * win] ** 2)) for i in range(n)])
    voiced = np.where(rms >= thresh)[0]
    if len(voiced) == 0:
        return {"t_speech_end_ms": 0.0, "internal_pauses_ms": [], "max_internal_pause_ms": 0.0}

    speech_end_ms = float((voiced[-1] + 1) * frame_ms)
    pauses = []
    for i in range(len(voiced) - 1):
        gap = (voiced[i + 1] - voiced[i] - 1) * frame_ms
        if gap >= frame_ms:
            pauses.append(float(gap))
    return {
        "t_speech_end_ms": speech_end_ms,
        "internal_pauses_ms": pauses,
        "max_internal_pause_ms": max(pauses) if pauses else 0.0,
    }


def to_pcm16_mono_16k(samples: np.ndarray, sr: int) -> bytes:
    if sr != TARGET_SR:
        n = int(round(len(samples) * TARGET_SR / sr))
        samples = np.interp(
            np.linspace(0, len(samples), n, endpoint=False),
            np.arange(len(samples)),
            samples,
        )
    peak = float(np.max(np.abs(samples))) or 1.0
    samples = samples / peak * 0.95  # normalize so levels match across clips
    return (samples * 32767).astype(np.int16).tobytes()


def main():
    FIXTURES.mkdir(exist_ok=True)
    model = Path(KOKORO_CACHE_DIR) / "kokoro-v1.0.onnx"
    voices = Path(KOKORO_CACHE_DIR) / "voices-v1.0.bin"
    _ensure_model_files(model, voices)
    kokoro = Kokoro(str(model), str(voices))

    manifest = []
    for name, text in UTTERANCES:
        samples, sr = kokoro.create(text, voice=VOICE, speed=1.0, lang="en-us")
        pcm = to_pcm16_mono_16k(np.asarray(samples, dtype=np.float32), sr)
        path = FIXTURES / f"{name}.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(TARGET_SR)
            wf.writeframes(pcm)

        duration_ms = len(pcm) / 2 / TARGET_SR * 1000
        speech = analyse_speech(pcm, TARGET_SR)
        manifest.append({
            "name": name,
            "file": f"{name}.wav",
            "text": text,
            "source": "synthetic:kokoro",
            "voice": VOICE,
            "sample_rate": TARGET_SR,
            "duration_ms": round(duration_ms, 1),
            "t_speech_end_ms": round(speech["t_speech_end_ms"], 1),
            "internal_pauses_ms": [round(p, 1) for p in speech["internal_pauses_ms"]],
            "max_internal_pause_ms": round(speech["max_internal_pause_ms"], 1),
            # The smallest VAD stop_secs that will not split this utterance.
            "min_workable_stop_secs": round(speech["max_internal_pause_ms"] / 1000, 2),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
        m = manifest[-1]
        print(f"  {name:20s} dur={duration_ms:7.1f}ms  speech_end={m['t_speech_end_ms']:7.1f}ms  "
              f"max_pause={m['max_internal_pause_ms']:6.1f}ms  "
              f"min_stop_secs>={m['min_workable_stop_secs']:.2f}")

    (FIXTURES / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\n{len(manifest)} fixtures -> {FIXTURES}/manifest.json")


if __name__ == "__main__":
    main()
