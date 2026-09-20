"""Deterministic WAV-in / captured-audio-out transport, for running without a mic.

Pipecat ships no file-based audio transport: `pipecat/transports/local/` has
only PyAudio (mic/speaker) and Tk. This is how `agent.py` runs a turn on a
machine where the microphone is awkward, and how the same utterance can be run
twice.

The critical detail is CADENCE. Silero VAD and the smart-turn model are
time-domain models that expect ~20ms frames arriving at real-time speed with
silence in between. Push a whole WAV as one frame and they see something no
microphone ever produces, and every turn-detection number becomes fiction.
So the input side is a virtual microphone: 20ms chunks, real-time paced,
followed by generated trailing silence so end-of-turn can actually fire.

                 t=0        20ms       40ms            speech_end
   feeder  ──►  [chunk]────[chunk]────[chunk]···[chunk]──[silence]···
                    │                                         │
                    ▼                                         ▼
              push_audio_frame()                     turn detector fires
                    │
              VAD ─► turn analyzer ─► STT ─► LLM ─► TTS
                                                     │
   capture ◄──────────────────────────────────────── write_audio_frame()
      │
      └─► every chunk timestamped on ONE monotonic clock, so
          t_first_audio is directly comparable to t_speech_end
"""

import asyncio
import time
import wave
from dataclasses import dataclass, field

from loguru import logger

from pipecat.frames.frames import (
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

CHUNK_MS = 20


@dataclass
class Capture:
    """Shared timing record for one run.

    Timing uses time.monotonic() because it cannot jump backwards. OpenTelemetry
    spans, however, are stamped with WALL CLOCK nanoseconds. The two are
    different epochs, so a capture timestamp cannot be compared to a span
    timestamp unless the offset between them is recorded. `wall_at_origin_ns`
    is that anchor, sampled once next to t_origin; `to_wall_ns()` does the
    conversion. Without this the end-to-end number and the span waterfall are
    two unrelated timelines.
    """

    t_origin: float = 0.0              # feeder emits its first chunk (monotonic)
    wall_at_origin_ns: int = 0         # time.time_ns() sampled at t_origin
    t_speech_end: float | None = None  # END of the last real-speech chunk
    t_turn_end: float | None = None    # detector declared the turn over
    t_first_audio: float | None = None  # first synthesized audio chunk out
    chunk_arrivals: list[float] = field(default_factory=list)  # feeder cadence
    out_chunks: list[tuple[float, bytes]] = field(default_factory=list)

    def rel(self, t: float | None) -> float | None:
        """Seconds since the feeder's first chunk."""
        return None if t is None else t - self.t_origin

    def to_wall_ns(self, t: float | None) -> int | None:
        """Monotonic reading -> wall-clock ns, so spans and capture can be joined."""
        if t is None:
            return None
        return self.wall_at_origin_ns + int((t - self.t_origin) * 1e9)

    @property
    def turn_detection_wait(self) -> float | None:
        """How long the agent waited in silence before deciding you were done.

        This is not compute. It is the silence timeout elapsing. It is
        routinely the largest single line in the budget, and it is a config
        constant, not a model property.
        """
        if self.t_speech_end is None or self.t_turn_end is None:
            return None
        return self.t_turn_end - self.t_speech_end

    @property
    def e2e_speech_end_to_first_audio(self) -> float | None:
        if self.t_speech_end is None or self.t_first_audio is None:
            return None
        return self.t_first_audio - self.t_speech_end

    def cadence_stats(self):
        """Actual inter-chunk gaps.

        p50 and cumulative drift both look excellent even when the feeder
        stalls and then bursts to catch up, because a late chunk and an early
        one cancel out. `late_chunks` and `burst_chunks` are reported so that
        failure mode is visible instead of averaged away.
        """
        a = self.chunk_arrivals
        if len(a) < 2:
            return {}
        gaps = [(a[i + 1] - a[i]) * 1000 for i in range(len(a) - 1)]
        gaps_sorted = sorted(gaps)
        expected = CHUNK_MS * (len(a) - 1) / 1000
        return {
            "chunks": len(a),
            "gap_ms_p50": gaps_sorted[len(gaps) // 2],
            "gap_ms_max": max(gaps),
            # a chunk arriving >5ms late, and one arriving >5ms early (catch-up)
            "late_chunks": sum(1 for g in gaps if g > CHUNK_MS + 5),
            "burst_chunks": sum(1 for g in gaps if g < CHUNK_MS - 5),
            "cumulative_drift_ms": ((a[-1] - a[0]) - expected) * 1000,
        }


class WavInputTransport(BaseInputTransport):
    """Plays a WAV into the pipeline at real-time 20ms cadence, then silence."""

    def __init__(
        self,
        params: TransportParams,
        wav_path: str,
        capture: Capture,
        trailing_silence_s: float = 3.0,
    ):
        super().__init__(params)
        self._wav_path = wav_path
        self._capture = capture
        self._trailing_silence_s = trailing_silence_s
        self._task: asyncio.Task | None = None
        self._sample_rate = 0
        self._channels = 1

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)
        if not self._task:
            self._task = self.create_task(self._feed())

    async def stop(self, frame):
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().stop(frame)

    async def cancel(self, frame):
        if self._task:
            await self.cancel_task(self._task)
            self._task = None
        await super().cancel(frame)

    async def _feed(self):
        with wave.open(self._wav_path, "rb") as wf:
            self._sample_rate = wf.getframerate()
            self._channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())

        bytes_per_frame = 2 * self._channels
        chunk_bytes = int(self._sample_rate * CHUNK_MS / 1000) * bytes_per_frame
        chunks = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
        silence = b"\x00" * chunk_bytes
        n_silence = int(self._trailing_silence_s * 1000 / CHUNK_MS)

        logger.info(
            f"feeder: {len(chunks)} speech chunks + {n_silence} silence chunks "
            f"@ {self._sample_rate}Hz"
        )

        from voice_agent.analysis import measurement
        speech_end_ms = measurement.speech_end_ms(self._wav_path)

        period = CHUNK_MS / 1000
        # Sample both clocks together. This pairing is the only thing that lets
        # capture timestamps be compared with OpenTelemetry span timestamps.
        self._capture.t_origin = time.monotonic()
        self._capture.wall_at_origin_ns = time.time_ns()
        if speech_end_ms is not None:
            self._capture.t_speech_end = self._capture.t_origin + speech_end_ms / 1000
        deadline = self._capture.t_origin

        for i, chunk in enumerate(chunks + [silence] * n_silence):
            now = time.monotonic()
            if now < deadline:
                await asyncio.sleep(deadline - now)
            emitted = time.monotonic()
            self._capture.chunk_arrivals.append(emitted)
            if speech_end_ms is None and i == len(chunks) - 1:
                # t0 for end-to-end latency: the moment the user stopped
                # talking, not the end of the file. The chunk is 20ms of audio,
                # so speech ends when the chunk FINISHES playing, not when it is
                # handed to the pipeline. Omitting the period understates t0 and
                # inflates every end-to-end number by up to one chunk.
                self._capture.t_speech_end = emitted + period

            if len(chunk) < chunk_bytes:
                chunk = chunk + b"\x00" * (chunk_bytes - len(chunk))

            await self.push_audio_frame(
                InputAudioRawFrame(
                    audio=chunk,
                    sample_rate=self._sample_rate,
                    num_channels=self._channels,
                )
            )
            deadline += period

        logger.info("feeder: done")


class CapturingOutputTransport(BaseOutputTransport):
    """Timestamps every outbound audio chunk on the shared clock."""

    def __init__(self, params: TransportParams, capture: Capture):
        super().__init__(params)
        self._capture = capture

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self.set_transport_ready(frame)

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        now = time.monotonic()
        if self._capture.t_first_audio is None:
            self._capture.t_first_audio = now
            logger.info(
                f"first bot audio @ +{self._capture.rel(now):.3f}s "
                f"(e2e {self._capture.e2e_speech_end_to_first_audio:.3f}s)"
            )
        self._capture.out_chunks.append((now, frame.audio))
        return True


class WavFileTransport(BaseTransport):
    """WAV in, timestamped audio out. Same output boundary for both modes."""

    def __init__(self, params: TransportParams, wav_path: str, capture: Capture,
                 trailing_silence_s: float = 3.0):
        super().__init__()
        self._params = params
        self._capture = capture
        self._input = WavInputTransport(params, wav_path, capture, trailing_silence_s)
        self._output = CapturingOutputTransport(params, capture)

    def input(self) -> BaseInputTransport:
        return self._input

    def output(self) -> BaseOutputTransport:
        return self._output
