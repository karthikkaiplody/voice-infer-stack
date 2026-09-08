"""Write the cold-open audio: what each build actually sounds like.

    uv run python clips.py --fixture 02-medium

Produces one WAV per build containing the user's question, then the REAL
measured silence, then the bot's reply. The silence is not padding and it is not
generated: it is the gap the run actually produced, reconstructed from the same
timestamps that produce the latency numbers.

Play these back to back and say nothing. The difference is the talk.
"""

import argparse
import asyncio
import wave
from pathlib import Path

import numpy as np

from config import CONFIG

OUT = Path("artifacts/clips")


def read_wav(path):
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        return np.frombuffer(pcm, dtype=np.int16), wf.getframerate()


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x
    n = int(round(len(x) * dst / src))
    return np.interp(
        np.linspace(0, len(x), n, endpoint=False), np.arange(len(x)), x
    ).astype(np.int16)


def write(path: Path, samples: np.ndarray, rate: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def assemble(user: np.ndarray, rate: int, gap_s: float, reply: np.ndarray):
    """user speech + the measured gap of real silence + the reply."""
    gap = np.zeros(int(gap_s * rate), dtype=np.int16)
    return np.concatenate([user, gap, reply])


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", default="02-medium")
    args = ap.parse_args()

    wav = f"fixtures/{args.fixture}.wav"
    user, user_rate = read_wav(wav)

    import naive
    import streaming

    # Warm up first. The first inference of a process pays model load and
    # graph compilation, and a cold run makes the sequential build look far
    # worse than it is. The cold-open clip has to be representative or the
    # whole talk is built on an exaggeration.
    print("  warming up ...")
    for _ in range(CONFIG.warmup_reps):
        await naive.run(wav)
        await streaming.one_run(-1, wav, CONFIG.vad_stop_secs)

    results = {}

    # BUILD 1 - the whole reply is synthesized before anything is heard.
    cap = await naive.run(wav)
    reply = (np.asarray(cap.reply_audio) * 32767).astype(np.int16)
    results["naive"] = (cap, resample(reply, cap.tts_rate, user_rate))

    # BUILD 2 - chunks arrive as they are produced; concatenating them gives
    # the same audio a listener would have heard.
    cap = await streaming.one_run(1, wav, CONFIG.vad_stop_secs)
    chunks = b"".join(c for _, c in cap.out_chunks)
    reply = np.frombuffer(chunks, dtype=np.int16)
    results["streaming"] = (cap, resample(reply, CONFIG.output_sample_rate, user_rate))

    print()
    for mode, (cap, reply) in results.items():
        gap = cap.e2e_speech_end_to_first_audio
        clip = assemble(user, user_rate, gap, reply)
        path = OUT / f"{args.fixture}-{mode}.wav"
        write(path, clip, user_rate)
        print(f"  {mode:<10} silence {gap * 1000:6.0f} ms   "
              f"total {len(clip) / user_rate:4.1f}s   {path}")

    a = results["naive"][0].e2e_speech_end_to_first_audio
    b = results["streaming"][0].e2e_speech_end_to_first_audio
    print(f"\n  The gap the audience hears: {a * 1000:.0f} ms vs {b * 1000:.0f} ms "
          f"({a / b:.1f}x)")
    print("  Do not trim the silence. The silence is the content.\n")


if __name__ == "__main__":
    asyncio.run(main())
