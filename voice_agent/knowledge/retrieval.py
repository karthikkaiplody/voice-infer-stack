"""The retrieval stage: find the relevant notes, then hand them to the model.

Sits between the user's finished turn and the LLM. When the user aggregator emits
its `LLMContextFrame`, this looks up the last user message in the agent's
knowledge, and forwards a COPY of the context in which that message carries the
matching notes ahead of the question. The notes go in the user turn rather than a
separate system message: the small local model answers from them far more
reliably there. The aggregator's own history is never touched, so notes from one
question do not pile up in the next.

Telemetry is timing and counts only. The question and the retrieved text are
content and are never emitted; `retrieval.top_source` is the repository's own
section label, a controlled code.
"""

from __future__ import annotations

import time
from typing import Any

from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent.knowledge.search import METHOD, BM25Index, Hit
from voice_agent.telemetry.contract import CONTROLLED_CODE_PATTERN

NOTES_PREAMBLE = (
    "Notes from the library's knowledge base for the next question. "
    "Answer only from these notes; if they do not answer it, say you do not have "
    "that information."
)
NO_NOTES = (
    "No notes in the knowledge base matched the next question. Say you do not "
    "have that information; do not guess."
)


def last_user_text(messages: list[Any]) -> tuple[int, str] | None:
    """Position and text of the most recent user message, if it has any text."""
    for position in range(len(messages) - 1, -1, -1):
        message = messages[position]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return position, content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            return position, " ".join(parts)
        return None
    return None


def notes_block(hits: list[Hit]) -> str:
    if not hits:
        return NO_NOTES
    body = "\n\n".join(f"[{n}] {hit.passage.render()}" for n, hit in enumerate(hits, 1))
    return f"{NOTES_PREAMBLE}\n\n{body}"


def with_notes(message: dict[str, Any], hits: list[Hit]) -> dict[str, Any]:
    """The user's message with the notes ahead of the question; other parts are kept."""
    block = notes_block(hits)
    content = message["content"]
    if isinstance(content, list):
        return {**message, "content": [{"type": "text", "text": f"{block}\n\nQuestion:"}, *content]}
    return {**message, "content": f"{block}\n\nQuestion: {content}"}


def augment(context: LLMContext, hits: list[Hit], position: int) -> LLMContext:
    """A new context: the same messages, with the notes carried by the question."""
    messages = list(context.get_messages())
    messages[position] = with_notes(messages[position], hits)
    return LLMContext(messages=messages, tools=context.tools, tool_choice=context.tool_choice)


class RetrievalProcessor(FrameProcessor):
    """Look up notes for each user turn, and report how long that took."""

    def __init__(self, index: BM25Index, *, top_k: int, **kwargs):
        super().__init__(**kwargs)
        self._index = index
        self._top_k = top_k
        # Set by `agent.build_worker`, which is what creates the turn identity.
        self.emitter = None

    async def process_frame(self, frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            frame = self._retrieve(frame)
        await self.push_frame(frame, direction)

    def _retrieve(self, frame: LLMContextFrame) -> LLMContextFrame:
        found = last_user_text(frame.context.get_messages())
        if found is None:
            return frame
        position, question = found
        started_wall = time.time_ns()
        started = time.perf_counter_ns()
        hits = self._index.search(question, self._top_k)
        elapsed = time.perf_counter_ns() - started
        self._report(started_wall, started_wall + elapsed, hits)
        return LLMContextFrame(context=augment(frame.context, hits, position))

    def _report(self, started_ns: int, completed_ns: int, hits: list[Hit]) -> None:
        """Best effort. Reporting must never cost the user an answer."""
        emitter = self.emitter
        if emitter is None:
            return
        try:
            emitter.emit("retrieval.started", started_ns, stage="retrieval")
            attributes: dict[str, Any] = {"retrieval.match_count": len(hits),
                                          "retrieval.method": METHOD}
            if hits and CONTROLLED_CODE_PATTERN.fullmatch(hits[0].passage.id):
                attributes["retrieval.top_source"] = hits[0].passage.id
            emitter.emit("retrieval.completed", completed_ns, stage="retrieval",
                         attributes=attributes)
        except Exception:
            pass
