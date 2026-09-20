"""The local server: fixture replay, safe SSE, loopback-only access, imports."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from voice_agent import paths
from voice_agent.server import live
from voice_agent.telemetry.contract import load_jsonl, validate_event

FIXTURES = paths.TELEMETRY_FIXTURES_DIR


@pytest.fixture
def fixture_mode(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "fixture")
    monkeypatch.setattr(live, "REPLAY_MAX_GAP_S", 0)
    monkeypatch.setattr(live, "CLIENTS", [])
    monkeypatch.setattr(live, "REPLAY_TASK", None)


def serve(coro_factory):
    """Run `coro_factory(client)` against a real app on a loopback port."""
    async def runner():
        async with TestClient(TestServer(live.create_app())) as client:
            return await coro_factory(client)
    return asyncio.run(runner())


async def read_messages(resp, until, limit=100):
    """SSE `data:` messages up to and including the first that satisfies `until`."""
    messages = []
    async for raw in resp.content:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        message = json.loads(line[5:])
        messages.append(message)
        if until(message) or len(messages) >= limit:
            break
    return messages


# ------------------------------------------------------------------- replay --

@pytest.mark.parametrize("scenario, filename", list(live.SCENARIOS.items()))
def test_replay_streams_each_fixture_event_for_event(fixture_mode, scenario, filename):
    expected = load_jsonl(FIXTURES / filename)

    async def go(client):
        async with client.get("/events") as stream:
            hello = await read_messages(stream, lambda m: m["kind"] == "hello")
            reply = await client.post(f"/replay/{scenario}")
            assert (await reply.json()) == {"ok": True, "scenario": scenario}
            rest = await asyncio.wait_for(read_messages(
                stream, lambda m: m["kind"] == "replay_finished"), timeout=5)
            return hello + rest

    messages = serve(go)
    kinds = [m["kind"] for m in messages]
    assert kinds[0] == "hello" and kinds[1] == "replay_reset"
    assert kinds[-1] == "replay_finished"
    events = [m["event"] for m in messages if m["kind"] == "telemetry"]
    assert events == expected               # same events, same order, unmodified
    for event in events:
        validate_event(event)


def test_replaying_twice_gives_identical_events(fixture_mode):
    async def once(client):
        async with client.get("/events") as stream:
            await read_messages(stream, lambda m: m["kind"] == "hello")
            await client.post("/replay/normal-completed")
            got = await asyncio.wait_for(read_messages(
                stream, lambda m: m["kind"] == "replay_finished"), timeout=5)
            return [m["event"] for m in got if m["kind"] == "telemetry"]

    assert serve(once) == serve(once)


def test_unknown_scenario_is_404_and_never_touches_the_filesystem(fixture_mode):
    async def go(client):
        bad = await client.post("/replay/..%2F..%2Fetc%2Fpasswd")
        unknown = await client.post("/replay/not-a-scenario")
        return bad.status, unknown.status, await unknown.json()

    bad_status, status, body = serve(go)
    assert bad_status == 404 and status == 404
    assert body == {"ok": False, "error": "unknown_scenario"}


def test_replay_is_refused_outside_fixture_mode(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "live")

    async def go(client):
        reply = await client.post("/replay/normal-completed")
        return reply.status, await reply.json()

    assert serve(go) == (409, {"ok": False, "error": "fixture_mode_required"})


def test_live_controls_are_refused_in_fixture_mode(fixture_mode):
    async def go(client):
        reply = await client.post("/start")
        return reply.status, await reply.json()

    assert serve(go) == (409, {"ok": False, "error": "live_mode_required"})


def test_replay_cap_is_declared_so_the_page_can_say_accelerated(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "fixture")
    assert live.hello()["replay"] == {"pacing": "accelerated", "max_gap_ms": 350}


# --------------------------------------------------------------- local only --

@pytest.mark.parametrize("method, path", [
    ("get", "/events"), ("get", "/"), ("post", "/replay/normal-completed"),
    ("post", "/start"), ("post", "/stop"), ("post", "/mode/live"),
])
def test_every_route_rejects_a_foreign_host(fixture_mode, method, path):
    async def go(client):
        reply = await getattr(client, method)(
            path, headers={"Host": "rebound.example.com"})
        return reply.status

    assert serve(go) == 403


@pytest.mark.parametrize("method, path", [
    ("get", "/events"), ("post", "/replay/normal-completed"), ("post", "/start"), ("post", "/mode/live"),
])
def test_every_route_rejects_a_foreign_origin(fixture_mode, method, path):
    async def go(client):
        host = f"{client.server.host}:{client.server.port}"
        reply = await getattr(client, method)(
            path, headers={"Host": host, "Origin": "http://evil.example.com"})
        return reply.status

    assert serve(go) == 403


def test_same_origin_requests_are_accepted(fixture_mode):
    async def go(client):
        host = f"{client.server.host}:{client.server.port}"
        reply = await client.post(
            "/replay/normal-completed",
            headers={"Host": host, "Origin": f"http://{host}"})
        return reply.status

    assert serve(go) == 200


def test_server_binds_loopback_only():
    source = Path(live.__file__).read_text()
    assert 'host = "127.0.0.1"' in source
    assert "0.0.0.0" not in source


# ------------------------------------------------------------------ privacy --

# `wait_for_transcript` is a legitimate boolean setting name, so "transcript"
# is deliberately not listed: this checks values, not vocabulary.
FORBIDDEN_FRAGMENTS = ("/Users", "/home", "artifacts/", "localhost", ".local",
                       "prompt", "api_key", "secret")


def test_fixture_hello_is_allowlisted_and_has_no_paths_or_hosts(fixture_mode):
    text = json.dumps(live.hello())
    for fragment in FORBIDDEN_FRAGMENTS:
        assert fragment not in text
    assert set(live.hello()) == {
        "kind", "now_ns", "mode", "configuration", "capabilities", "agent", "scenarios",
        "running", "replay"}


def test_export_configuration_drops_everything_unsafe():
    raw = {
        "snapshot_id": "config_abc123",
        "source_revision": "0123456789abcdef",
        "provider_ids": {"stt": "mlx", "llm": "ollama", "tts": "kokoro"},
        "model_ids": {"stt": "Users/someone/models/private",
                      "llm": "llama3.2:1b", "tts": "af_heart",
                      "note": "sk-abcdefghijklmnopqrstuvwxyz0123456789ABCD"},
        "endpointing_settings": {"vad_stop_secs": 0.4, "use_smart_turn": False,
                                 "bad": float("nan")},
        "hardware_class": "personal_computer",
        "cpu_architecture": "arm64",
        "os_version": "my-laptop.local",
        "execution_engine_id": "pipecat",
        "privacy_mode": "anything the caller says",
        "prompt": "You are a helpful assistant",
        "api_key": "sk-secret",
        "audio_device": "Someone's MacBook Microphone",
        "path": "/Users/someone/project",
    }
    clean = live.export_configuration(raw)
    assert set(clean) <= set(live._CONFIG_KEYS)
    assert clean["model_ids"] == {"llm": "llama3.2:1b", "tts": "af_heart"}
    assert clean["endpointing_settings"] == {"vad_stop_secs": 0.4,
                                             "use_smart_turn": False}
    assert clean["os_version"] is None
    assert clean["privacy_mode"] == "metadata-only"
    text = json.dumps(clean)
    for fragment in ("sk-", "Users", "prompt", "api_key", "MacBook", "my-laptop"):
        assert fragment not in text


def test_live_hello_uses_the_same_safe_export(monkeypatch):
    from voice_agent.pipeline import agent
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    message = live.hello()
    assert message["configuration"] == live.export_configuration(
        agent.CONFIG_SNAPSHOT.as_dict())
    assert "replay" not in message and message["scenarios"] == []
    text = json.dumps(message)
    for fragment in ("/Users", "artifacts/", "prompt_revision"):
        assert fragment not in text


# ------------------------------------------------------------ capabilities --

def test_live_capabilities_admit_what_is_not_instrumented(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent=""))
    caps = live.capabilities()
    assert caps["stages"] == {
        "user_speech": True, "endpointing": True, "stt": True, "retrieval": False,
        "llm": True, "tools": False, "tts": True, "output_transport": True}
    assert caps["outcomes"] == {
        "completed": True, "interrupted": False, "failed": True}


def test_live_retrieval_is_instrumented_only_for_an_agent_with_knowledge(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent="library"))
    assert live.capabilities()["stages"]["retrieval"] is True


def test_fixture_capabilities_cover_every_stage_and_outcome(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "fixture")
    caps = live.capabilities()
    assert all(caps["stages"].values()) and all(caps["outcomes"].values())


# ------------------------------------------------------------------ assets --

def test_missing_ui_build_is_a_readable_503(fixture_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(live, "UI", tmp_path)

    async def go(client):
        reply = await client.get("/")
        return reply.status, await reply.text()

    status, text = serve(go)
    assert status == 503 and "make ui-build" in text


def test_built_ui_is_served(fixture_mode, monkeypatch, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<!doctype html><title>ok</title>")
    (tmp_path / "assets" / "a.js").write_text("export {}")
    monkeypatch.setattr(live, "UI", tmp_path)

    async def go(client):
        page = await client.get("/")
        asset = await client.get("/assets/a.js")
        return page.status, asset.status

    assert serve(go) == (200, 200)


# ----------------------------------------------------------------- imports --

def test_fixture_mode_needs_neither_pipecat_nor_the_agent():
    """Replay must work for someone who has installed nothing model-related."""
    code = (
        "import sys\n"
        "sys.modules['pipecat'] = None\n"        # any `import pipecat...` now fails
        "from voice_agent.server import live\n"
        "live.SERVER_MODE = 'fixture'\n"
        "live.hello()\n"
        "live.create_app()\n"
        "assert 'voice_agent.pipeline.agent' not in sys.modules and 'voice_agent.telemetry.tracing' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=paths.REPO_ROOT)
    assert done.returncode == 0, done.stderr


# ------------------------------------------------------------------- agent --

def test_the_hello_names_the_agent_only_in_live_mode(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "fixture")
    assert live.hello()["agent"] is None                    # the fixtures are synthetic
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent=""))
    assert live.hello()["agent"] is None
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent="library"))
    assert live.hello()["agent"] == {"id": "library", "title": "Riverside Public Library information line"}


def test_the_agent_summary_carries_no_notes_prompt_or_greeting(monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent="library"))
    text = json.dumps(live.hello()["agent"]).lower()
    for leaked in ("9am", "greeting", "hello, this is", "you are the automated"):
        assert leaked not in text


def test_the_grounded_answer_scenario_is_offered_and_replays(fixture_mode):
    assert "grounded-answer" in live.hello()["scenarios"]
    events = load_jsonl(FIXTURES / live.SCENARIOS["grounded-answer"])
    assert {e["schema_version"] for e in events} == {"1.1.0"}
    assert [e["event_name"] for e in events if e["event_name"].startswith("retrieval.")] == [
        "retrieval.started", "retrieval.completed"]


# -------------------------------------------------------------- mode switch --

@pytest.fixture
def switchable(monkeypatch, fixture_mode):
    """Fixture mode, with live mode's Pipecat-heavy startup replaced by a marker."""
    started = []
    monkeypatch.setattr(live, "start_tracing", lambda: started.append(True))
    monkeypatch.setattr(live, "RUNNING", False)
    return started


def test_the_page_can_switch_to_live_and_back_and_every_page_is_told(switchable, monkeypatch):
    monkeypatch.setattr(live, "ACTIVE_CONFIG", dataclasses.replace(live.ACTIVE_CONFIG, agent="library"))

    async def go(client):
        async with client.get("/events") as stream:
            first = await read_messages(stream, lambda m: m["kind"] == "hello")
            to_live = await client.post("/mode/live")
            live_hello = await read_messages(stream, lambda m: m["kind"] == "hello")
            back = await client.post("/mode/fixture")
            fixture_hello = await read_messages(stream, lambda m: m["kind"] == "hello")
            return first[-1], (await to_live.json()), live_hello[-1], (await back.json()), fixture_hello[-1]

    first, to_live, live_hello, back, fixture_hello = serve(go)
    assert first["mode"] == "fixture" and first["scenarios"]
    assert to_live == {"ok": True, "mode": "live"}
    assert live_hello["mode"] == "live" and live_hello["scenarios"] == [] and live_hello["tuning"]
    assert live_hello["agent"]["id"] == "library"
    assert back == {"ok": True, "mode": "fixture"}
    assert fixture_hello["mode"] == "fixture" and fixture_hello["agent"] is None
    assert switchable == [True]                       # live mode's startup ran once, on the first switch


def test_switching_to_the_mode_already_in_use_changes_nothing(switchable):
    async def go(client):
        reply = await client.post("/mode/fixture")
        return await reply.json()

    assert serve(go) == {"ok": True, "mode": "fixture"}
    assert switchable == [] and live.SERVER_MODE == "fixture"


def test_it_is_refused_while_the_live_pipeline_is_running(switchable, monkeypatch):
    monkeypatch.setattr(live, "SERVER_MODE", "live")
    monkeypatch.setattr(live, "RUNNING", True)

    async def go(client):
        reply = await client.post("/mode/fixture")
        return reply.status, await reply.json()

    assert serve(go) == (409, {"ok": False, "error": "stop_first"})
    assert live.SERVER_MODE == "live"                 # the microphone is never orphaned


def test_an_unknown_mode_is_404(switchable):
    async def go(client):
        reply = await client.post("/mode/cloud")
        return reply.status, await reply.json()

    assert serve(go) == (404, {"ok": False, "error": "unknown_mode"})
    assert live.SERVER_MODE == "fixture"


def test_switching_away_cancels_a_replay_in_progress(switchable, monkeypatch):
    async def go(client):
        async def forever():
            await asyncio.sleep(60)
        monkeypatch.setattr(live, "REPLAY_TASK", asyncio.create_task(forever()))
        task = live.REPLAY_TASK
        await client.post("/mode/live")
        await asyncio.sleep(0)
        return task.cancelled() or task.cancelling() > 0

    assert serve(go) is True


# ------------------------------------------------------------ console logging --

@pytest.fixture
def restore_logging():
    yield
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr)


def test_the_console_does_not_print_spoken_or_generated_text(capsys, restore_logging):
    """Pipecat logs transcripts and TTS text at DEBUG; the terminal must not show them."""
    from loguru import logger
    live.configure_logging()
    logger.debug("Transcription: [CANARY spoken words]")
    logger.debug("Generating TTS [CANARY generated words]")
    logger.info("mode: live")
    err = capsys.readouterr().err
    assert "CANARY" not in err
    assert "mode: live" in err                      # ordinary INFO output is still there


def test_main_sets_up_console_logging_before_anything_can_log():
    source = Path(live.__file__).read_text()
    assert "    configure_logging()\n    SERVER_MODE = args.mode" in source          # the call, not the def
