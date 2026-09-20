"""A microphone that delivers nothing is reported, and a quiet room is not."""

from __future__ import annotations

import asyncio
import struct

from pipecat.frames.frames import InputAudioRawFrame, TextFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection

from voice_agent.server.input_watch import InputSilenceObserver

RATE = 16000
CHUNK = RATE // 50                                   # 20 ms


def chunk(value: int = 0) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=struct.pack("<h", value) * CHUNK, sample_rate=RATE, num_channels=1)


def feed(observer, frames):
    async def go():
        for frame in frames:
            await observer.on_push_frame(FramePushed(
                source=None, destination=None, frame=frame,
                direction=FrameDirection.DOWNSTREAM, timestamp=0))
    asyncio.run(go())


def watch(after_secs=2.0):
    changes = []
    return InputSilenceObserver(changes.append, after_secs=after_secs), changes


def test_two_seconds_of_exact_silence_is_reported_once():
    observer, changes = watch()
    feed(observer, [chunk(0) for _ in range(150)])   # 3 s
    assert changes == [True]


def test_a_short_gap_of_silence_is_not_reported():
    observer, changes = watch()
    feed(observer, [chunk(0) for _ in range(90)])    # 1.8 s
    assert changes == []


def test_a_quiet_room_is_not_silence():
    """One count of noise is enough: real microphones are never exactly zero."""
    observer, changes = watch()
    feed(observer, [chunk(1 if n % 2 else 0) for n in range(500)])
    assert changes == []


def test_sound_returning_clears_the_report_and_silence_can_be_reported_again():
    observer, changes = watch()
    feed(observer, [chunk(0) for _ in range(120)] + [chunk(40)] + [chunk(0) for _ in range(120)])
    assert changes == [True, False, True]


def test_a_frame_seen_at_every_hop_is_counted_once():
    observer, changes = watch()
    frame = chunk(0)
    feed(observer, [frame] * 500)                    # the same frame, reported 500 times
    assert changes == []                             # 20 ms of silence, not 10 s


def test_other_frames_are_ignored():
    observer, changes = watch()
    feed(observer, [TextFrame(text="hello")] * 200)
    assert changes == []
