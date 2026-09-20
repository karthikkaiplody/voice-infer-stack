"""Settings read from the environment: `VOICE_<NAME>` overrides `Config.<name>`."""

from __future__ import annotations

from voice_agent import config


def test_the_audio_device_is_a_number_not_the_text_of_one(monkeypatch):
    """`VOICE_AUDIO_DEVICE=3 make live` used to arrive as the string '3'."""
    monkeypatch.setenv("VOICE_AUDIO_DEVICE", "3")
    assert config._from_env().audio_device == 3


def test_an_empty_audio_device_means_the_system_default(monkeypatch):
    monkeypatch.setenv("VOICE_AUDIO_DEVICE", "")
    assert config._from_env().audio_device is None


def test_other_types_are_still_converted(monkeypatch):
    monkeypatch.setenv("VOICE_VAD_STOP_SECS", "0.3")
    monkeypatch.setenv("VOICE_USE_SMART_TURN", "1")
    monkeypatch.setenv("VOICE_RETRIEVAL_TOP_K", "5")
    monkeypatch.setenv("VOICE_LLM_MODEL", "qwen2.5:0.5b")
    loaded = config._from_env()
    assert (loaded.vad_stop_secs, loaded.use_smart_turn, loaded.retrieval_top_k, loaded.llm_model) == (
        0.3, True, 5, "qwen2.5:0.5b")
