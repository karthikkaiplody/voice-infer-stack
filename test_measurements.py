"""Measurement invariants.

These do not test that the code runs. They test that the NUMBERS ARE NOT LYING,
because the failure that ruins this project is not a crash, it is a chart that
is quietly wrong and ends up on a conference slide.

Everything here runs against the committed reference traces, so it needs no
models and no GPU:

    uv run pytest -q
"""

import json
import re
from pathlib import Path

import pytest

import analysis
import budget
from config import CONFIG

TRACES = Path("artifacts/reference-traces.jsonl")
FIXTURES = Path("fixtures/manifest.json")
COMPUTE = ["stt", "llm", "tts"]


@pytest.fixture(scope="module")
def spans():
    if not TRACES.exists():
        pytest.skip(f"{TRACES} missing; run `make bench`")
    return budget.load_spans(TRACES)


@pytest.fixture(scope="module")
def runs(spans):
    """Measured turns only. Warmup is excluded everywhere it matters."""
    out = []
    for win in budget.windows(spans):
        s = budget.summarize(spans, win)
        if s:
            out.append((win, s))
    return out


def by_mode(runs, mode):
    return [(w, s) for w, s in runs if w["attributes"].get("mode") == mode]


# --- the window itself ------------------------------------------------------

def test_every_run_has_a_budget_window(runs):
    """Without the window span there is no defensible definition of latency."""
    assert runs, "no e2e.speech_end_to_first_audio spans found"


def test_both_builds_present(runs):
    modes = {w["attributes"].get("mode") for w, _ in runs}
    assert {"naive", "streaming"} <= modes, f"only found {modes}"


def test_window_duration_is_positive_and_plausible(runs):
    for w, s in runs:
        ms = s["wall_ms"]
        assert 0 < ms < 60_000, f"implausible end-to-end: {ms} ms"


# --- the core claims --------------------------------------------------------

def test_naive_compute_stages_do_not_overlap(runs):
    """Build 1's whole point is that nothing runs concurrently.

    Checked by intersecting time INTERVALS. Summing durations cannot tell
    concurrency apart from a stage that simply ran long.
    """
    for w, s in by_mode(runs, "naive"):
        for a, b, ov in s["overlaps"]:
            assert ov < 50, (
                f"naive build overlapped {a}/{b} by {ov:.0f} ms; "
                f"it is supposed to be sequential"
            )


def test_streaming_actually_overlaps_llm_and_tts(runs):
    """Build 2's whole point. If this fails the comparison is meaningless."""
    found = False
    for w, s in by_mode(runs, "streaming"):
        for a, b, ov in s["overlaps"]:
            if {a, b} == {"llm", "tts"} and ov > 50:
                found = True
    assert found, "streaming build showed no llm/tts overlap"


def test_no_stage_escapes_its_window(runs):
    """Clipping must hold: a stage cannot cost more than the whole turn."""
    for w, s in runs:
        for row in s["rows"]:
            assert row["duration_ms"] <= s["wall_ms"] + 1, (
                f"{row['stage']} ({row['duration_ms']:.0f} ms) exceeds the "
                f"{s['wall_ms']:.0f} ms window"
            )


def test_overlap_never_exceeds_the_smaller_stage(runs):
    """A sanity bound on the interval maths itself."""
    for w, s in runs:
        durations = {r["stage"]: r["duration_ms"] for r in s["rows"]}
        for a, b, ov in s["overlaps"]:
            assert ov <= min(durations[a], durations[b]) + 1, (
                f"{a}/{b} overlap {ov:.0f} ms exceeds the shorter stage"
            )


# --- the thing that would invalidate the whole comparison -------------------

def norm(text):
    return re.sub(r"[^a-z0-9 ]", "", (text or "").lower()).strip()


def test_both_builds_transcribed_the_same_words(spans):
    """If the builds heard different things, they are not comparable.

    Compared on normalized text: raw comparison fails on punctuation and
    casing, which is noise, not drift.
    """
    seen = {}
    for s in spans:
        t = s["attributes"].get("transcript")
        if s["name"] == "stt" and t:
            seen.setdefault(norm(t), 0)
            seen[norm(t)] += 1
    assert len(seen) <= 1, f"builds produced different transcripts: {list(seen)}"


def test_no_discarded_work_in_the_reference_run(runs):
    """More than one llm span in a turn means a false endpoint.

    The agent answered a fragment, threw it away, and answered again. That work
    lands inside the measured window and inflates every number.
    """
    for w, s in runs:
        for row in s["rows"]:
            if row["stage"] == "llm":
                assert row["count"] == 1, (
                    f"{w['attributes'].get('mode')} made {row['count']} LLM "
                    f"calls in one turn: work was discarded"
                )


# --- fixtures ---------------------------------------------------------------

def test_speech_end_is_before_end_of_file():
    """t0 is the end of SPEECH. Fixtures carry trailing silence by design, and
    using file duration as t0 would understate every latency number."""
    if not FIXTURES.exists():
        pytest.skip("no fixture manifest")
    for f in json.loads(FIXTURES.read_text()):
        assert f["t_speech_end_ms"] < f["duration_ms"], (
            f"{f['name']}: speech runs to the end of the file, so the turn "
            f"detector has no silence to work with"
        )


def test_fixture_pause_is_compatible_with_stop_secs():
    """A pause longer than stop_secs makes the agent answer a fragment."""
    if not FIXTURES.exists():
        pytest.skip("no fixture manifest")
    for f in json.loads(FIXTURES.read_text()):
        if f["name"] in ("02-medium",):
            assert f["min_workable_stop_secs"] <= CONFIG.vad_stop_secs, (
                f"{f['name']} needs stop_secs >= "
                f"{f['min_workable_stop_secs']}, config has "
                f"{CONFIG.vad_stop_secs}"
            )


# --- statistics -------------------------------------------------------------

def test_median_is_a_real_median():
    """sorted(x)[len(x)//2] is the upper-middle value, not the median, for any
    even sample. That bug silently shifted every number in an earlier sweep."""
    assert analysis.median([1, 2, 3, 4]) == 2.5
    assert analysis.median([1, 2, 3]) == 2
    assert analysis.median([]) is None
