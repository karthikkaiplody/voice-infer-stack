"""Record your own utterance and add it to the fixture set.

    uv run python -m voice_agent.tools.record --name my-question --seconds 6

Speak, stop, and let the recording run on for a second or so. That trailing
silence is not waste: it is what the turn detector needs in order to decide you
have finished. The script measures where your speech actually ends and stores
that as t0 for every latency number, so trailing silence never inflates the
result.

Uses ffmpeg rather than a Python audio library so there is no extra dependency
and no portaudio build step.
"""

import argparse
import hashlib
import json
import subprocess
import wave
from pathlib import Path

from voice_agent.tools.make_fixtures import FIXTURES, TARGET_SR, analyse_speech


def list_devices():
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation",
         "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    print(out.stderr)


def record(path: Path, seconds: float, device: str):
    print(f"\n  Recording {seconds:.0f}s to {path.name} ...")
    print("  Speak now, then STOP TALKING and let it run on.\n")
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "avfoundation", "-i", device,
         "-ar", str(TARGET_SR), "-ac", "1", "-t", str(seconds), str(path)],
        check=True,
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", default="my-question")
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--device", default=":0", help="ffmpeg avfoundation input")
    ap.add_argument("--text", default="", help="what you said, for the record")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        list_devices()
        return

    FIXTURES.mkdir(exist_ok=True)
    path = FIXTURES / f"{args.name}.wav"
    record(path, args.seconds, args.device)

    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
    speech = analyse_speech(pcm, TARGET_SR)
    duration_ms = len(pcm) / 2 / TARGET_SR * 1000

    if speech["t_speech_end_ms"] == 0:
        raise SystemExit("  No speech detected. Check your mic and try again.")
    if speech["t_speech_end_ms"] >= duration_ms - 200:
        print("  WARNING: speech runs to the end of the recording. Turn "
              "detection needs trailing silence; re-record with more time.")

    manifest_path = FIXTURES / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest = [m for m in manifest if m["name"] != args.name]
    manifest.append({
        "name": args.name,
        "file": path.name,
        "text": args.text,
        "source": "recorded:microphone",
        "sample_rate": TARGET_SR,
        "duration_ms": round(duration_ms, 1),
        "t_speech_end_ms": round(speech["t_speech_end_ms"], 1),
        "internal_pauses_ms": [round(p, 1) for p in speech["internal_pauses_ms"]],
        "max_internal_pause_ms": round(speech["max_internal_pause_ms"], 1),
        "min_workable_stop_secs": round(speech["max_internal_pause_ms"] / 1000, 2),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    })
    manifest.sort(key=lambda m: m["name"])
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    m = manifest[-1] if manifest[-1]["name"] == args.name else next(
        x for x in manifest if x["name"] == args.name)
    print(f"  saved {path}")
    print(f"    duration            {m['duration_ms']:.0f} ms")
    print(f"    speech ends at      {m['t_speech_end_ms']:.0f} ms   <- t0 for latency")
    print(f"    trailing silence    {m['duration_ms'] - m['t_speech_end_ms']:.0f} ms")
    print(f"    longest pause       {m['max_internal_pause_ms']:.0f} ms")
    print(f"    needs stop_secs >=  {m['min_workable_stop_secs']:.2f} s "
          f"(below this the agent will cut you off)")
    print(f"\n  Now run:  make trace FIXTURE={args.name}")


if __name__ == "__main__":
    main()
