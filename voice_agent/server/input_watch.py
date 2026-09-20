"""Notice a microphone that delivers nothing, and say so.

A voice agent whose input is silent looks exactly like one that is working: it
greets you, then waits forever. The usual cause is the system's default input
being a virtual device (a mixer or a loopback) with no source routed to it, or a
missing microphone permission. Neither raises an error.

A real microphone always carries a little noise, so audio that is *exactly* zero,
for a couple of seconds, means nothing is arriving at all. That needs no volume
threshold, and a quiet room cannot trigger it.

Only the amount of silence is looked at, never the audio itself.
"""

from __future__ import annotations

from typing import Callable

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed

SILENT_AFTER_SECS = 2.0


class InputSilenceObserver(BaseObserver):
    """Calls `on_change(True)` after `after_secs` of exact silence, `on_change(False)` when sound returns.

    Subclasses BaseObserver directly, like every observer here (see
    `telemetry/tracing.TurnSpanObserver`).
    """

    def __init__(self, on_change: Callable[[bool], None], *,
                 after_secs: float = SILENT_AFTER_SECS, **kwargs):
        super().__init__(**kwargs)
        self._on_change = on_change
        self._after = after_secs
        self._silent_for = 0.0
        self._reported = False
        self._last_frame_id: int | None = None

    async def on_push_frame(self, data: FramePushed):
        frame = data.frame
        # The same frame is reported at every hop down the pipeline; count it once.
        if not isinstance(frame, InputAudioRawFrame) or frame.id == self._last_frame_id:
            return
        self._last_frame_id = frame.id
        audio = frame.audio
        if not audio:
            return
        if audio.count(0) == len(audio):
            self._silent_for += len(audio) / (2 * frame.num_channels * frame.sample_rate)
            if not self._reported and self._silent_for >= self._after:
                self._reported = True
                self._on_change(True)
        else:
            self._silent_for = 0.0
            if self._reported:
                self._reported = False
                self._on_change(False)
