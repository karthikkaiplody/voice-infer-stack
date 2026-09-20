"""The retrieval stage: what the model is shown, what telemetry says, where it sits."""

from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from pipecat.frames.frames import LLMContextFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.filters.identity_filter import IdentityFilter
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voice_agent.pipeline import agent
from voice_agent.knowledge import agents
from voice_agent.pipeline import factory
from voice_agent.knowledge import retrieval
from voice_agent.config import Config
from voice_agent.knowledge.search import METHOD
from voice_agent.knowledge.retrieval import RetrievalProcessor, augment, last_user_text, notes_block, with_notes
from voice_agent.telemetry.contract import RuntimeEventEmitter, validate_trace

LIBRARY = agents.load_agent("library")
S = 1_000_000_000


def context(*messages):
    return LLMContext(messages=list(messages))


def processor(top_k=3):
    return RetrievalProcessor(LIBRARY.index, top_k=top_k)


def emitter_with_turn():
    sink = []
    emitter = RuntimeEventEmitter(configuration_snapshot_id="cfg", workload_fixture_id="w",
                                  session_id="s", conversation_id="c", callback=sink.append)
    emitter.start_turn(1)
    return emitter, sink


# ------------------------------------------------------------------ the question --

def test_the_last_user_message_is_the_question():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"}, {"role": "user", "content": "second"}]
    assert last_user_text(messages) == (3, "second")


def test_text_parts_of_a_structured_message_are_joined():
    messages = [{"role": "user", "content": [{"type": "text", "text": "when"},
                                             {"type": "image_url", "image_url": {"url": "x"}},
                                             {"type": "text", "text": "open"}]}]
    assert last_user_text(messages) == (0, "when open")


@pytest.mark.parametrize("messages", [[], [{"role": "system", "content": "x"}],
                                      [{"role": "user", "content": None}]])
def test_no_question_means_no_lookup(messages):
    assert last_user_text(messages) is None


# --------------------------------------------------------------- what the model sees --

def test_notes_travel_in_the_question_turn_and_history_is_kept():
    ctx = context({"role": "system", "content": "rules"}, {"role": "user", "content": "hi"},
                  {"role": "assistant", "content": "hello"},
                  {"role": "user", "content": "what time do you close on sunday"})
    hits = LIBRARY.index.search("what time do you close on sunday", 3)
    messages = augment(ctx, hits, 3).get_messages()
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[:3] == ctx.get_messages()[:3]
    content = messages[3]["content"]
    assert content.startswith(retrieval.NOTES_PREAMBLE)
    assert "12pm to 5pm on Sunday" in content
    assert content.endswith("\n\nQuestion: what time do you close on sunday")


def test_a_structured_question_keeps_its_other_parts():
    image = {"type": "image_url", "image_url": {"url": "x"}}
    message = {"role": "user", "content": [{"type": "text", "text": "when open"}, image]}
    parts = with_notes(message, LIBRARY.index.search("when open", 1))["content"]
    assert parts[0]["text"].startswith(retrieval.NOTES_PREAMBLE) and parts[0]["text"].endswith("Question:")
    assert parts[1:] == message["content"]


def test_the_shared_context_is_never_changed():
    ctx = context({"role": "user", "content": "how long can I keep a book"})
    before = json.dumps(ctx.get_messages())
    result = augment(ctx, LIBRARY.index.search("how long can I keep a book", 2), 0)
    assert json.dumps(ctx.get_messages()) == before and result is not ctx


def test_a_question_nothing_matches_tells_the_model_not_to_guess():
    assert notes_block([]) == retrieval.NO_NOTES
    assert "do not guess" in retrieval.NO_NOTES
    message = with_notes({"role": "user", "content": "pirate joke"}, [])
    assert message == {"role": "user", "content": f"{retrieval.NO_NOTES}\n\nQuestion: pirate joke"}


def test_the_notes_are_numbered_and_carry_their_headings():
    hits = LIBRARY.index.search("late fee", 3)
    assert notes_block(hits).split("\n\n")[1].startswith("[1] Fines and late fees:")


def test_the_processor_forwards_a_copy_with_the_notes_and_leaves_the_original(monkeypatch):
    ctx = context({"role": "user", "content": "can I book a study room"})
    original = json.dumps(ctx.get_messages())
    forwarded = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        forwarded.append((frame, direction))

    async def base(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", base)
    p = processor()
    monkeypatch.setattr(p, "push_frame", push)
    asyncio.run(p.process_frame(LLMContextFrame(context=ctx), FrameDirection.DOWNSTREAM))
    frame, _ = forwarded[0]
    assert isinstance(frame, LLMContextFrame) and frame.context is not ctx
    assert "two weeks ahead" in frame.context.get_messages()[0]["content"]
    assert json.dumps(ctx.get_messages()) == original


def test_other_frames_and_upstream_context_frames_pass_through_untouched(monkeypatch):
    forwarded = []

    async def push(frame, direction=FrameDirection.DOWNSTREAM):
        forwarded.append(frame)

    async def base(self, frame, direction):
        return None

    monkeypatch.setattr(FrameProcessor, "process_frame", base)
    p = processor()
    monkeypatch.setattr(p, "push_frame", push)
    text = TextFrame(text="hello")
    upstream = LLMContextFrame(context=context({"role": "user", "content": "hi"}))
    asyncio.run(p.process_frame(text, FrameDirection.DOWNSTREAM))
    asyncio.run(p.process_frame(upstream, FrameDirection.UPSTREAM))
    assert forwarded == [text, upstream]


# ---------------------------------------------------------------------- telemetry --

def test_retrieval_reports_timing_a_count_and_the_top_section_only():
    p = processor()
    p.emitter, sink = emitter_with_turn()
    ctx = context({"role": "user", "content": "what time do you close on sunday"})
    p._retrieve(LLMContextFrame(context=ctx))
    started, completed = sink[1], sink[2]
    assert [started["event_name"], completed["event_name"]] == ["retrieval.started", "retrieval.completed"]
    assert completed["timestamp_ns"] >= started["timestamp_ns"]
    assert completed["attributes"] == {"retrieval.match_count": 1 + len(
        [h for h in LIBRARY.index.search("what time do you close on sunday", 3)][1:]),
        "retrieval.method": METHOD, "retrieval.top_source": "library.opening-hours"}
    validate_trace([*sink, {**sink[0], "event_id": "evt_end", "event_name": "turn.completed",
                            "timestamp_ns": completed["timestamp_ns"] + S, "attributes": {}}])


def test_no_question_or_answer_text_ever_reaches_telemetry():
    p = processor()
    p.emitter, sink = emitter_with_turn()
    p._retrieve(LLMContextFrame(context=context(
        {"role": "user", "content": "what time do you close on sunday canaryword"})))
    text = json.dumps(sink).lower()
    for leaked in ("canaryword", "close on sunday", "12pm", "opening hours", "9am"):
        assert leaked not in text


def test_no_match_reports_zero_and_no_source():
    p = processor()
    p.emitter, sink = emitter_with_turn()
    forwarded = p._retrieve(LLMContextFrame(context=context({"role": "user", "content": "tell me a pirate joke"})))
    assert sink[2]["attributes"] == {"retrieval.match_count": 0, "retrieval.method": METHOD}
    assert forwarded.context.get_messages()[0]["content"].startswith(retrieval.NO_NOTES)


def test_a_source_label_that_is_not_a_safe_code_is_left_out(monkeypatch):
    p = processor()
    p.emitter, sink = emitter_with_turn()
    hit = LIBRARY.index.search("late fee", 1)[0]
    bad = SimpleNamespace(passage=SimpleNamespace(id="Has Spaces/And Slashes", render=hit.passage.render), score=1)
    monkeypatch.setattr(p._index, "search", lambda q, k: [bad])
    p._retrieve(LLMContextFrame(context=context({"role": "user", "content": "late fee"})))
    assert "retrieval.top_source" not in sink[2]["attributes"]


def test_reporting_problems_never_cost_the_user_an_answer():
    p = processor()
    p.emitter = SimpleNamespace(emit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    frame = p._retrieve(LLMContextFrame(context=context({"role": "user", "content": "late fee"})))
    assert "10 cents" in json.dumps(frame.context.get_messages())


def test_it_works_with_no_emitter_and_with_no_active_turn():
    LLMContextFrame(context=context({"role": "user", "content": "late fee"}))
    assert processor()._retrieve(LLMContextFrame(context=context({"role": "user", "content": "late fee"})))
    p = processor()
    p.emitter = RuntimeEventEmitter(configuration_snapshot_id="c", workload_fixture_id="w",
                                    session_id="s", conversation_id="c", callback=lambda e: None)
    p._retrieve(LLMContextFrame(context=context({"role": "user", "content": "late fee"})))
    assert p.emitter.drops == {"no_active_turn": 2}


def test_top_k_limits_how_many_notes_the_model_gets():
    question = "library book loan renew fine card room open"
    matches = len(LIBRARY.index.search(question, 99))
    assert matches >= 4
    for k in (1, 2, 3, 4):
        frame = processor(top_k=k)._retrieve(LLMContextFrame(context=context({"role": "user", "content": question})))
        notes = frame.context.get_messages()[0]["content"]
        assert len(re.findall(r"^\[\d+\]", notes, flags=re.M)) == k


# ------------------------------------------------------------------- the pipeline --

def build(monkeypatch, config):
    for name in ("make_stt", "make_llm", "make_tts", "make_vad"):
        monkeypatch.setattr(factory, name, lambda c=None, _n=name: f"<{_n}>")
    monkeypatch.setattr(agent, "LLMContext", lambda: object())
    monkeypatch.setattr(agent, "LLMContextAggregatorPair", lambda *a, **k: ("<user_agg>", "<assistant_agg>"))
    monkeypatch.setattr(agent, "Pipeline", lambda processors: SimpleNamespace(processors=processors))
    transport = SimpleNamespace(input=lambda: "<in>", output=lambda: "<out>")
    return agent.build_pipeline(transport, config=config)


def test_the_library_agent_puts_retrieval_between_the_user_turn_and_the_model(monkeypatch):
    pipeline = build(monkeypatch, Config(agent="library"))
    names = [p if isinstance(p, str) else "retrieval" for p in pipeline.processors]
    assert names == ["<in>", "<make_stt>", "<user_agg>", "retrieval", "<make_llm>", "<make_tts>", "<out>", "<assistant_agg>"]
    assert isinstance(pipeline.voice_retriever, RetrievalProcessor)


def test_the_original_configuration_has_no_retrieval_stage(monkeypatch):
    pipeline = build(monkeypatch, Config())
    assert pipeline.voice_retriever is None
    assert all(isinstance(p, str) for p in pipeline.processors) and len(pipeline.processors) == 7


def test_retrieval_depth_comes_from_the_config(monkeypatch):
    pipeline = build(monkeypatch, Config(agent="library", retrieval_top_k=5))
    assert pipeline.voice_retriever._top_k == 5


def test_the_worker_connects_retrieval_to_its_own_turn_identity():
    sink = []
    pipeline = Pipeline([IdentityFilter()])
    pipeline.voice_retriever = processor()
    worker = agent.build_worker(pipeline, mode="live", event_callback=sink.append,
                                config=Config(agent="library"))
    assert pipeline.voice_retriever.emitter is worker.voice_event_emitter
    worker.voice_event_emitter.start_turn(1)
    pipeline.voice_retriever._retrieve(LLMContextFrame(context=context({"role": "user", "content": "late fee"})))
    assert [e["event_name"] for e in sink] == ["user_speech.started", "retrieval.started", "retrieval.completed"]
    assert len({e["identities"]["turn_id"] for e in sink}) == 1


def test_the_model_is_given_the_agents_prompt_not_the_inline_one():
    llm = factory.make_llm(Config(agent="library"))
    assert llm._settings.system_instruction == LIBRARY.prompt
    assert factory.make_llm(Config())._settings.system_instruction == Config().system_prompt


def test_the_snapshot_names_the_agent_revision_and_retrieval_depth():
    library = agent.snapshot_for(Config(agent="library"))
    assert dict(library.behavior_settings)["retrieval_top_k"] == 3
    assert library.prompt_revision_id == LIBRARY.prompt_revision_id
    assert agent.snapshot_for(Config()).prompt_revision_id == "library-information-v1"
    assert library.snapshot_id != agent.snapshot_for(Config()).snapshot_id
    assert agent.snapshot_for(Config(agent="library", retrieval_top_k=5)).snapshot_id != library.snapshot_id
