"""Agents and knowledge: the file format, the checks, and the ranking."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_agent.knowledge import agents
from voice_agent.knowledge.agents import AgentError, load_agent
from voice_agent.config import CONFIG, Config
from voice_agent.knowledge.search import BM25Index, MAX_PASSAGE_CHARS, split_markdown, stem, tokenize
from voice_agent.telemetry.contract import CONTROLLED_CODE_PATTERN


# ---------------------------------------------------------------- the library --

def test_the_library_agent_loads_with_its_knowledge_and_greeting():
    agent = load_agent("library")
    assert agent.title == "Riverside Public Library information line"
    assert agent.has_knowledge and len(agent.passages) == 6
    assert agent.greeting.startswith("Hello, this is the Riverside Public Library")
    assert "answer using only those notes" in agent.prompt.lower()


def test_no_library_fact_was_lost_moving_it_out_of_the_prompt():
    """The facts that used to live in Config.system_prompt are still there, now searchable."""
    knowledge = " ".join(p.text for p in load_agent("library").passages).lower()
    for fact in ("9am to 8pm monday to friday", "10am to 6pm on saturday", "12pm to 5pm on sunday",
                 "three weeks", "renewed twice online", "10 cents a day", "5 dollars",
                 "free with proof of address", "two weeks ahead"):
        assert fact in knowledge, fact


def test_the_facts_are_in_the_knowledge_and_not_in_the_prompt():
    prompt = load_agent("library").prompt.lower()
    assert "9am" not in prompt and "10 cents" not in prompt and "proof of address" not in prompt


@pytest.mark.parametrize("question, section", [
    ("what time do you close on Sunday", "opening-hours"),
    ("are you open on weekends", "opening-hours"),
    ("how long can I keep a book", "borrowing-and-loans"),
    ("can I renew my loan", "renewals"),
    ("how much is the late fee", "fines-and-late-fees"),
    ("what happens if I return a book late", "fines-and-late-fees"),
    ("how do I get a library card", "library-cards"),
    ("can I book a study room", "study-rooms"),
])
def test_library_questions_find_the_right_passage_first(question, section):
    hits = load_agent("library").index.search(question, 3)
    assert hits and hits[0].passage.id == f"library.{section}"


@pytest.mark.parametrize("question", ["tell me a joke about pirates", "do you have any events", "", "the a of"])
def test_questions_the_notes_do_not_cover_find_nothing(question):
    assert load_agent("library").index.search(question, 3) == []


def test_every_passage_id_is_safe_to_put_in_telemetry():
    for passage in load_agent("library").passages:
        assert CONTROLLED_CODE_PATTERN.fullmatch(passage.id), passage.id


# ------------------------------------------------------------------ knowledge --

def test_markdown_is_split_by_heading_and_the_document_title_is_not_a_topic():
    passages = split_markdown("guide", "# Guide\nintro text\n\n## Alpha\nfirst\n\n## Beta\nsecond\n")
    assert [(p.id, p.heading) for p in passages] == [
        ("guide.overview", ""), ("guide.alpha", "Alpha"), ("guide.beta", "Beta")]


def test_nested_headings_keep_their_trail():
    passages = split_markdown("g", "# T\n## Outer\n### Inner\nbody\n")
    assert passages[0].heading == "Outer > Inner"


def test_long_sections_are_split_and_ids_stay_unique():
    body = " ".join(f"Sentence number {i} says something useful." for i in range(60))
    passages = split_markdown("g", f"## Long\n{body}\n")
    assert len(passages) > 1
    assert all(len(p.text) <= MAX_PASSAGE_CHARS for p in passages)
    assert len({p.id for p in passages}) == len(passages)


def test_markdown_emphasis_is_removed_before_the_model_sees_it():
    assert split_markdown("g", "## A\nThis is **bold** and `code`.\n")[0].text == "This is bold and code."


@pytest.mark.parametrize("a, b", [("fines", "fine"), ("books", "book"), ("renewed", "renew"),
                                  ("borrowing", "borrow"), ("closing", "close"), ("hours", "hour")])
def test_the_stemmer_meets_common_word_forms(a, b):
    assert stem(a) == stem(b)


def test_stopwords_do_not_count_as_matches():
    assert tokenize("What is the time") == [stem("time")]


def test_ranking_is_deterministic_and_ties_keep_file_order():
    passages = split_markdown("g", "## One\nsame words here\n\n## Two\nsame words here\n")
    index = BM25Index(passages)
    assert [h.passage.heading for h in index.search("words", 5)] == ["One", "Two"]
    assert index.search("words", 5) == index.search("words", 5)


def test_a_heading_match_outranks_the_same_word_buried_in_prose():
    index = BM25Index(split_markdown("g", "## Parking\nCars go here.\n\n## Hours\nOpen daily, parking is behind.\n"))
    assert index.search("parking", 2)[0].passage.heading == "Parking"


def test_top_k_limits_results_and_zero_returns_none():
    index = load_agent("library").index
    assert len(index.search("library book", 2)) <= 2
    assert index.search("library book", 0) == []


def test_an_empty_index_is_harmless():
    assert BM25Index([]).search("anything", 3) == []


# ------------------------------------------------------------- the file format --

def make(tmp_path, toml: str, *, prompt="Be helpful.", knowledge=None):
    base = tmp_path / "demo"
    (base / "knowledge").mkdir(parents=True)
    (base / "agent.toml").write_text(toml)
    if prompt is not None:
        (base / "prompt.md").write_text(prompt)
    for name, text in (knowledge or {}).items():
        (base / name).write_text(text)
    return tmp_path


GOOD = 'id = "demo"\ntitle = "Demo"\nprompt = "prompt.md"\n[knowledge]\nfiles = ["knowledge/a.md"]\n'


def test_a_minimal_agent_without_knowledge_is_valid(tmp_path):
    root = make(tmp_path, 'id = "demo"\ntitle = "Demo"\nprompt = "prompt.md"\n')
    agent = load_agent("demo", root)
    assert not agent.has_knowledge and agent.greeting is None and agent.prompt == "Be helpful."


def test_adding_an_agent_is_adding_a_folder(tmp_path):
    root = make(tmp_path, GOOD, knowledge={"knowledge/a.md": "## Cats\nCats purr.\n"})
    assert agents.list_agents(root) == ["demo"]
    agent = load_agent("demo", root)
    assert agent.has_knowledge and agent.index.search("cat", 1)[0].passage.id == "a.cats"


@pytest.mark.parametrize("toml, message", [
    ('id = "demo"\ntitle = "D"\nprompt = "prompt.md"\ncolour = "red"', "unknown key"),
    ('id = "other"\ntitle = "D"\nprompt = "prompt.md"', "folder is 'demo'"),
    ('id = "demo"\nprompt = "prompt.md"', "missing `title`"),
    ('id = "demo"\ntitle = ""\nprompt = "prompt.md"', "non-empty string"),
    ('id = "demo"\ntitle = "D"', "expected a file name"),
    ('id = "demo"\ntitle = "D"\nprompt = "missing.md"', "does not exist"),
    ('id = "demo"\ntitle = "D"\nprompt = "../outside.md"', "outside the agent's folder"),
    ('id = "demo"\ntitle = "D"\nprompt = "/etc/passwd"', "outside the agent's folder"),
    ('id = "demo"\ntitle = "D"\nprompt = "prompt.md"\n[knowledge]\nfiles = ["nope.md"]', "does not exist"),
    ('id = "demo"\ntitle = "D"\nprompt = "prompt.md"\n[knowledge]\nurl = "x"', "only accepts"),
    ('id = "demo"\ntitle = "D"\nprompt = "prompt.md"\n[knowledge]\nfiles = "a.md"', "must be a list"),
    ('this is = not toml [', "not valid TOML"),
])
def test_a_mistake_in_the_files_is_reported_in_plain_words(tmp_path, toml, message):
    root = make(tmp_path, toml)
    (tmp_path / "outside.md").write_text("secret")
    with pytest.raises(AgentError, match=re.escape(message)):
        load_agent("demo", root)


def test_a_symlink_cannot_reach_outside_the_agent_folder(tmp_path):
    root = make(tmp_path, GOOD, knowledge={"knowledge/a.md": "x"})
    secret = tmp_path / "secret.md"
    secret.write_text("private")
    link = root / "demo" / "knowledge" / "b.md"
    link.symlink_to(secret)
    (root / "demo" / "agent.toml").write_text(GOOD.replace("a.md", "b.md"))
    with pytest.raises(AgentError, match="outside the agent's folder"):
        load_agent("demo", root)


@pytest.mark.parametrize("bad", ["", "Demo", "../demo", "de mo", "a" * 40, "-demo"])
def test_agent_names_are_restricted(tmp_path, bad):
    with pytest.raises(AgentError, match="not valid"):
        load_agent(bad, tmp_path)


def test_an_unknown_agent_lists_what_exists(tmp_path):
    make(tmp_path, GOOD, knowledge={"knowledge/a.md": "x"})
    with pytest.raises(AgentError, match="Available: demo"):
        load_agent("ghost", tmp_path)


def test_size_limits_and_encoding_are_enforced(tmp_path, monkeypatch):
    root = make(tmp_path, GOOD, knowledge={"knowledge/a.md": "x" * 50})
    monkeypatch.setattr(agents, "MAX_KNOWLEDGE_BYTES", 10)
    with pytest.raises(AgentError, match="the limit is 10"):
        load_agent("demo", root)
    monkeypatch.undo()
    (root / "demo" / "knowledge" / "a.md").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(AgentError, match="not UTF-8"):
        load_agent("demo", root)


def test_duplicate_section_names_get_distinct_ids(tmp_path):
    root = make(tmp_path, GOOD, knowledge={"knowledge/a.md": "## Same\none\n\n## Same\ntwo\n"})
    ids = [p.id for p in load_agent("demo", root).passages]
    assert ids == ["a.same", "a.same-2"]


# -------------------------------------------------------------------- revision --

def test_the_revision_changes_when_any_agent_file_changes(tmp_path):
    root = make(tmp_path, GOOD, knowledge={"knowledge/a.md": "## A\none\n"})
    first = load_agent("demo", root).prompt_revision_id
    assert load_agent("demo", root).prompt_revision_id == first
    (root / "demo" / "knowledge" / "a.md").write_text("## A\ntwo\n")
    assert load_agent("demo", root).prompt_revision_id != first
    (root / "demo" / "prompt.md").write_text("Different.")
    assert load_agent("demo", root).prompt_revision_id != first


def test_the_revision_is_a_safe_identifier():
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", load_agent("library").prompt_revision_id)


# ------------------------------------------------------------------ selection --

def test_no_agent_means_the_original_prompt_and_revision():
    assert agents.agent_for(CONFIG if not CONFIG.agent else Config()) is None
    legacy = Config()
    assert agents.system_prompt_for(legacy) == legacy.system_prompt
    assert agents.prompt_revision_for(legacy) == "library-information-v1"


def test_selecting_the_library_agent_switches_prompt_and_revision():
    config = Config(agent="library")
    assert agents.system_prompt_for(config) == load_agent("library").prompt
    assert agents.system_prompt_for(config) != config.system_prompt
    assert agents.prompt_revision_for(config).startswith("library-")


def test_an_unknown_agent_selected_by_config_fails_with_a_clear_message():
    with pytest.raises(AgentError, match="no agent named 'ghost'"):
        agents.agent_for(SimpleNamespace(agent="ghost"))
