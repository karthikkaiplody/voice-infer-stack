"""Measurement invariants.

These do not test that the code runs. They test that the NUMBERS ARE NOT LYING,
because the failure that ruins this project is not a crash, it is a chart that
is quietly wrong and ends up on a conference slide.

Everything here runs against the committed reference traces, so it needs no
models and no GPU:

    uv run pytest -q

`artifacts/scheduling-comparison.jsonl` is a sanitized fixed recording. It
holds the same synthetic workload run two ways, one overlapping its
stages and one strictly sequential, which is what makes it the right input for
these tests: the interval maths has something to get wrong. Nothing in the repo
produces it any more, and nothing should regenerate it.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from voice_agent import paths
from voice_agent.analysis import measurement
from voice_agent.analysis import budget
from voice_agent.config import CONFIG

TRACES = paths.ARTIFACTS_DIR / "scheduling-comparison.jsonl"
FIXTURES = paths.AUDIO_FIXTURES_DIR / "manifest.json"
COMPUTE = ["stt", "llm", "tts"]


@pytest.fixture(scope="module")
def spans():
    if not TRACES.exists():
        pytest.skip(f"{TRACES} missing")
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


def test_legacy_budget_is_clearly_noncomparable(capsys, spans, runs):
    window, summary = runs[0]
    budget.render(summary, "test")
    assert budget.LEGACY_NOTICE in capsys.readouterr().out


def test_committed_viewer_matches_generated_legacy_output(tmp_path):
    generated = tmp_path / "viewer.html"
    subprocess.run(
        [sys.executable, "-m", "voice_agent.analysis.viewer", "--traces",
         str(paths.ARTIFACTS_DIR / "reference-traces.jsonl"), "--out", str(generated)],
        check=True, capture_output=True, text=True, cwd=paths.REPO_ROOT)
    assert budget.LEGACY_NOTICE in generated.read_text()
    assert generated.read_text() == (paths.ARTIFACTS_DIR / "viewer.html").read_text()


def test_only_budget_artifacts_have_legacy_window_markers():
    marked = (
        paths.ARTIFACTS_DIR / "reference-traces.jsonl",
        paths.ARTIFACTS_DIR / "scheduling-comparison.jsonl",
    )
    for path in marked:
        windows = budget.windows(budget.load_spans(path))
        assert windows
        assert all(w["attributes"].get("measured_as")
                   == "tts_first_synthesized_sample_legacy" for w in windows)
    false_endpoint = budget.load_spans(
        paths.ARTIFACTS_DIR / "turn-detection" / "false-endpoint-traces.jsonl")
    assert budget.windows(false_endpoint) == []


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

def _models(spans, stage, window):
    return {sp["attributes"].get("gen_ai.request.model")
            for sp in spans
            if sp["name"] == stage
            and sp["end_time_ns"] > window[0]
            and sp["start_time_ns"] < window[1]}


def test_both_builds_used_the_same_models(spans, runs):
    """The comparison is void if the builds ran different models.

    This is not hypothetical. streaming.py hardcoded llama3.2:3b while naive.py
    read CONFIG, so one tuned run compared a 1b naive build against a 3b
    streaming build and still got reported as "only scheduling differs".
    """
    per_mode = {}
    for w, _ in runs:
        mode = w["attributes"].get("mode")
        window = (w["start_time_ns"], w["end_time_ns"])
        for stage in ("llm", "stt"):
            got = {m for m in _models(spans, stage, window) if m}
            if got:
                per_mode.setdefault(stage, {}).setdefault(mode, set()).update(got)

    for stage, modes in per_mode.items():
        distinct = set().union(*modes.values())
        assert len(distinct) == 1, (
            f"{stage} ran on different models across builds: "
            + ", ".join(f"{m}={sorted(v)}" for m, v in modes.items())
        )


def test_streaming_reads_config_rather_than_hardcoding(spans, runs):
    """The streaming build must report the model config.py asked for."""
    for w, _ in runs:
        if w["attributes"].get("mode") != "streaming":
            continue
        window = (w["start_time_ns"], w["end_time_ns"])
        got = {m for m in _models(spans, "llm", window) if m}
        if got:
            assert got == {CONFIG.llm_model}, (
                f"streaming used {sorted(got)} but config.py says "
                f"{CONFIG.llm_model}"
            )


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

def test_config_has_every_field_the_builds_use():
    """A missing config field breaks a build that no other test exercises.

    `trailing_silence_s` was deleted by an over-wide edit and stayed broken
    through two commits, because the invariants read committed traces and never
    run the pipeline. This asserts the surface the pipeline actually imports.
    """
    from dataclasses import fields as dc_fields

    required = {
        "stt_model", "llm_model", "tts_voice",
        "input_sample_rate", "output_sample_rate", "chunk_ms",
        "vad_stop_secs", "trailing_silence_s",
        "use_smart_turn", "user_speech_timeout", "wait_for_transcript",
        "stt_ttfs_p99", "filter_incomplete_user_turns",
        "llm_temperature", "llm_seed", "llm_max_tokens", "system_prompt",
        "warmup_reps", "measured_reps",
    }
    have = {f.name for f in dc_fields(CONFIG)}
    assert required <= have, f"config.py is missing {sorted(required - have)}"


def test_every_module_imports_cleanly():
    """Catches a broken module before a run does, which costs minutes.

    Every module in the package is imported, so a new file is covered without
    anyone remembering to list it. `server.live` matters most: it is the main
    artifact, the file most likely to be edited, and nothing else here would
    notice if an import in it broke.
    """
    import importlib
    import pkgutil

    import voice_agent

    for module in pkgutil.walk_packages(voice_agent.__path__, "voice_agent."):
        importlib.import_module(module.name)


def test_median_is_a_real_median():
    """sorted(x)[len(x)//2] is the upper-middle value, not the median, for any
    even sample. That bug silently shifted every number in an earlier sweep."""
    assert measurement.median([1, 2, 3, 4]) == 2.5
    assert measurement.median([1, 2, 3]) == 2
    assert measurement.median([]) is None
