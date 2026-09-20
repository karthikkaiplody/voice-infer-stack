"""Agents: a folder that says who the assistant is and what it knows.

    agents/library/
        agent.toml          who it is, and which files it uses
        prompt.md           how it should behave (persona and rules, no facts)
        knowledge/*.md      what it knows; searched on every turn

Adding an agent is adding a folder. `agents/README.md` walks through it.

An agent's files are checked when it loads, with messages written for the person
who edited them. Paths must stay inside the agent's folder, unknown keys are
errors (a typo should not silently do nothing), and sizes are capped so a stray
file cannot end up in the prompt.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from voice_agent import paths
from voice_agent.knowledge.search import BM25Index, Passage, split_markdown

AGENTS_DIR = paths.AGENTS_DIR
LEGACY_PROMPT_REVISION = "library-information-v1"

_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_TOP_KEYS = {"id", "title", "description", "prompt", "greeting", "knowledge"}
_KNOWLEDGE_KEYS = {"files"}
MAX_PROMPT_BYTES = 8_000
MAX_KNOWLEDGE_BYTES = 400_000
MAX_KNOWLEDGE_FILES = 20


class AgentError(ValueError):
    """The agent's files are wrong. The message says what to fix."""


@dataclass(frozen=True)
class Agent:
    id: str
    title: str
    description: str
    prompt: str
    greeting: str | None
    passages: tuple[Passage, ...] = ()
    revision: str = ""
    index: BM25Index | None = field(default=None, compare=False, repr=False)

    @property
    def has_knowledge(self) -> bool:
        return self.index is not None and len(self.index) > 0

    @property
    def prompt_revision_id(self) -> str:
        """A safe identifier that changes whenever the agent's files do."""
        return f"{self.id}-{self.revision}"


def _text_file(base: Path, relative: object, what: str, limit: int) -> tuple[str, bytes]:
    if not isinstance(relative, str) or not relative.strip():
        raise AgentError(f"{what}: expected a file name, got {relative!r}")
    path = (base / relative).resolve()
    if base.resolve() not in path.parents:
        raise AgentError(f"{what}: {relative!r} points outside the agent's folder")
    if not path.is_file():
        raise AgentError(f"{what}: {relative!r} does not exist in {base.name}/")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise AgentError(f"{what}: {relative!r} is {len(raw):,} bytes; the limit is {limit:,}")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError as error:
        raise AgentError(f"{what}: {relative!r} is not UTF-8 text") from error


def _string(table: dict, key: str, *, required: bool, limit: int) -> str | None:
    if key not in table:
        if required:
            raise AgentError(f"agent.toml: missing `{key}`")
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise AgentError(f"agent.toml: `{key}` must be a non-empty string")
    if len(value) > limit:
        raise AgentError(f"agent.toml: `{key}` is longer than {limit} characters")
    return value.strip()


def load_agent(agent_id: str, root: Path = AGENTS_DIR) -> Agent:
    """Load and check one agent. Raises `AgentError` with a fixable message."""
    if not _ID.fullmatch(agent_id or ""):
        raise AgentError(
            f"agent name {agent_id!r} is not valid: use lowercase letters, digits, "
            "hyphens and underscores, starting with a letter or digit")
    base = Path(root) / agent_id
    manifest = base / "agent.toml"
    if not manifest.is_file():
        available = ", ".join(list_agents(root)) or "none"
        raise AgentError(f"no agent named {agent_id!r} (looked for {agent_id}/agent.toml). "
                         f"Available: {available}")
    try:
        table = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise AgentError(f"agent.toml is not valid TOML: {error}") from error
    unknown = set(table) - _TOP_KEYS
    if unknown:
        raise AgentError(f"agent.toml: unknown key(s) {sorted(unknown)}; allowed: {sorted(_TOP_KEYS)}")
    if table.get("id") != agent_id:
        raise AgentError(f"agent.toml: `id` is {table.get('id')!r} but the folder is {agent_id!r}")

    title = _string(table, "title", required=True, limit=80)
    description = _string(table, "description", required=False, limit=300) or ""
    greeting = _string(table, "greeting", required=False, limit=300)
    prompt_text, prompt_raw = _text_file(base, table.get("prompt"), "prompt", MAX_PROMPT_BYTES)

    knowledge_table = table.get("knowledge", {})
    if not isinstance(knowledge_table, dict) or set(knowledge_table) - _KNOWLEDGE_KEYS:
        raise AgentError("agent.toml: [knowledge] only accepts `files = [...]`")
    files = knowledge_table.get("files", [])
    if not isinstance(files, list) or len(files) > MAX_KNOWLEDGE_FILES:
        raise AgentError(f"agent.toml: `knowledge.files` must be a list of at most {MAX_KNOWLEDGE_FILES} files")

    passages: list[Passage] = []
    digest = hashlib.sha256()
    for part in (agent_id.encode(), prompt_raw, (greeting or "").encode()):
        digest.update(hashlib.sha256(part).digest())
    used = 0
    for name in files:
        text, raw = _text_file(base, name, "knowledge", MAX_KNOWLEDGE_BYTES)
        used += len(raw)
        if used > MAX_KNOWLEDGE_BYTES:
            raise AgentError(f"knowledge files total more than {MAX_KNOWLEDGE_BYTES:,} bytes")
        digest.update(hashlib.sha256(raw).digest())
        passages.extend(split_markdown(Path(name).stem, text))
    ids = [p.id for p in passages]
    if len(ids) != len(set(ids)):
        raise AgentError("two knowledge sections produce the same passage id; rename a heading")

    return Agent(
        id=agent_id, title=title, description=description, prompt=prompt_text.strip(),
        greeting=greeting, passages=tuple(passages), revision=digest.hexdigest()[:8],
        index=BM25Index(passages) if passages else None)


def list_agents(root: Path = AGENTS_DIR) -> list[str]:
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "agent.toml").is_file())


@lru_cache(maxsize=8)
def _cached(agent_id: str) -> Agent:
    return load_agent(agent_id)


def agent_for(config) -> Agent | None:
    """The agent a config selects, or `None` for the original inline prompt.

    Loaded once per process; edit an agent's files, then restart.
    """
    return _cached(config.agent) if config.agent else None


def system_prompt_for(config) -> str:
    agent = agent_for(config)
    return agent.prompt if agent is not None else config.system_prompt


def prompt_revision_for(config) -> str:
    agent = agent_for(config)
    return agent.prompt_revision_id if agent is not None else LEGACY_PROMPT_REVISION
