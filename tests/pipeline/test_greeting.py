"""The greeting: who says it, when, and what it must not disturb."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.workers.runner import WorkerRunner

from voice_agent.config import Config
from voice_agent.knowledge import agents
from voice_agent.pipeline import agent

LIBRARY = agents.load_agent("library")


def test_an_agent_with_a_greeting_says_it_as_speech_that_joins_the_conversation():
    frames = agent.greeting_frames(Config(agent="library"))
    assert len(frames) == 1 and isinstance(frames[0], TTSSpeakFrame)
    assert frames[0].text == LIBRARY.greeting
    assert frames[0].append_to_context is True        # the model knows it already said hello


def test_no_agent_means_no_greeting_so_the_benchmark_is_untouched():
    assert agent.greeting_frames(Config()) == []


def test_an_agent_without_a_greeting_says_nothing(monkeypatch):
    monkeypatch.setattr(agents, "agent_for", lambda config: SimpleNamespace(greeting=None))
    assert agent.greeting_frames(Config(agent="quiet")) == []


def test_frames_queued_before_the_pipeline_starts_arrive_after_it_has():
    """The order `live.run_agent` relies on: queue the greeting, then run."""
    seen = []

    class Recorder(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TTSSpeakFrame):
                seen.append(frame.text)
            await self.push_frame(frame, direction)

    async def go():
        worker = PipelineWorker(Pipeline([Recorder()]))
        await worker.queue_frames([*agent.greeting_frames(Config(agent="library")), EndFrame()])
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await asyncio.wait_for(runner.run(), timeout=10)

    asyncio.run(go())
    assert seen == [LIBRARY.greeting]
