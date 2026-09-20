"""Markdown knowledge, cut into passages and ranked with BM25.

No dependencies and no model: it runs in well under a millisecond on a small
knowledge base, works offline, and gives the same answer every time. That makes
the retrieval stage in the waterfall a real measurement you can reason about.

BM25 matches WORDS, not meaning. "When do you close?" finds the opening-hours
passage because both say "close" or "open" and "hours", but "late fees" will not
find a passage that only says "fines". The fix is in the writing: put the words
people actually say into the knowledge file. `agents/README.md` covers this.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

METHOD = "bm25"
MAX_PASSAGE_CHARS = 700

_STOPWORDS = frozenset("""
a an and are as at be but by can could did do does for from had has have how i if in
into is it its me my of on or our so than that the their them then there these they
this to us was we were what when where which who why will with would you your please
tell about know want need
""".split())

_HEADING = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Passage:
    """One retrievable piece of a knowledge file."""

    id: str          # `source.section`, a controlled code safe to put in telemetry
    source: str      # file stem
    heading: str
    text: str        # what the model is shown

    def render(self) -> str:
        return f"{self.heading}: {self.text}" if self.heading else self.text


@dataclass(frozen=True)
class Hit:
    passage: Passage
    score: float


def _slug(text: str, limit: int) -> str:
    return _SLUG_JUNK.sub("-", text.lower()).strip("-")[:limit].strip("-") or "section"


def _clean(text: str) -> str:
    """Plain prose for the model: no emphasis markers or code fences."""
    text = re.sub(r"[*_`]{1,3}", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _split_long(text: str) -> list[str]:
    """Cut an over-long section at paragraph, then sentence, boundaries."""
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]
    pieces: list[str] = []
    current = ""
    for unit in re.split(r"\n\s*\n|(?<=[.!?])\s+", text):
        unit = unit.strip()
        if not unit:
            continue
        if current and len(current) + 1 + len(unit) > MAX_PASSAGE_CHARS:
            pieces.append(current)
            current = unit
        else:
            current = f"{current} {unit}".strip()
    if current:
        pieces.append(current)
    return pieces


def split_markdown(source: str, markdown: str) -> list[Passage]:
    """Passages from one markdown file: one per heading, long sections split."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    trail: list[tuple[int, str]] = []
    for line in markdown.splitlines():
        match = _HEADING.match(line)
        if match:
            level, title = len(match.group(1)), _clean(match.group(2))
            trail = [(n, t) for n, t in trail if n < level] + [(level, title)]
            # A top-level `# Title` names the document; it is not a topic, so it
            # is left out of the heading and its own text is the overview.
            sections.append((" > ".join(t for n, t in trail if n >= 2), []))
        else:
            sections[-1][1].append(line)

    passages: list[Passage] = []
    seen: Counter[str] = Counter()
    for heading, lines in sections:
        body = _clean("\n".join(lines))
        if not body:
            continue
        for piece in _split_long(body):
            base = f"{_slug(source, 20)}.{_slug(heading, 36) if heading else 'overview'}"
            seen[base] += 1
            pid = base if seen[base] == 1 else f"{base}-{seen[base]}"
            passages.append(Passage(pid, source, heading, piece))
    return passages


def stem(word: str) -> str:
    """A deliberately tiny stemmer so plurals and -ing/-ed forms meet."""
    if word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    for suffix in ("ing", "ed"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) > 3:
        word = word[:-1]
    return word


def tokenize(text: str) -> list[str]:
    return [stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS]


class BM25Index:
    """Okapi BM25 over a fixed list of passages."""

    def __init__(self, passages: list[Passage], *, k1: float = 1.5, b: float = 0.75):
        self.passages = tuple(passages)
        self._k1, self._b = k1, b
        # A heading is the best statement of what a passage is about: count it twice.
        self._terms = [Counter(tokenize(f"{p.heading} {p.heading} {p.text}")) for p in passages]
        self._lengths = [sum(t.values()) for t in self._terms]
        self._average = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        document_frequency: Counter[str] = Counter()
        for terms in self._terms:
            document_frequency.update(terms.keys())
        n = len(passages)
        self._idf = {
            term: math.log(1 + (n - df + 0.5) / (df + 0.5))
            for term, df in document_frequency.items()
        }

    def __len__(self) -> int:
        return len(self.passages)

    def search(self, query: str, k: int) -> list[Hit]:
        """Up to `k` passages sharing at least one word with the query, best first."""
        wanted = set(tokenize(query))
        if not wanted or k <= 0 or not self.passages:
            return []
        scored: list[tuple[float, int]] = []
        for index, terms in enumerate(self._terms):
            score = 0.0
            for term in wanted:
                frequency = terms.get(term, 0)
                if not frequency:
                    continue
                norm = 1 - self._b + self._b * self._lengths[index] / (self._average or 1)
                score += self._idf[term] * frequency * (self._k1 + 1) / (frequency + self._k1 * norm)
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], item[1]))     # ties keep file order
        return [Hit(self.passages[i], round(s, 6)) for s, i in scored[:k]]
