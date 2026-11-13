from __future__ import annotations

import json
import os
import stat

from heyclicky.config import CONFIG_DIR_ENV, load_config


def test_defaults_are_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    config = load_config()
    assert config.get("llm.provider") == "openrouter"
    assert config.get("llm.reasoning") == "off"
    assert config.get("capture.max_dimension") == 1280
    assert config.get("hotkey.combo") == "ctrl+alt"
    assert config.get("hotkey.mode") == "auto"
    assert config.get("hotkey.fallback_combo") == "ctrl+shift"
    assert config.path == tmp_path / "config.json"


def test_save_is_private_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    config = load_config()
    config.set("llm.groq.api_key", "gsk_secret")
    config.set("ui.onboarding_completed", True)
    config.save()

    path = tmp_path / "config.json"
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    payload = json.loads(path.read_text())
    assert payload["llm"]["groq"]["api_key"] == "gsk_secret"

    reloaded = load_config()
    assert reloaded.get("ui.onboarding_completed") is True


def test_env_keys_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-env")
    monkeypatch.setenv("HEYCLICKY_LLM_PROVIDER", "gemini")
    config = load_config()
    assert config.get("llm.groq.api_key") == "from-env"
    assert config.get("llm.gemini.api_key") == "gemini-env"
    assert config.get("llm.provider") == "gemini"


def test_ollama_host_without_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.5:11434")
    config = load_config()
    assert config.get("llm.ollama.base_url") == "http://192.168.1.5:11434"


def test_malformed_config_is_backed_up(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    path = tmp_path / "config.json"
    path.write_text("{not json")
    config = load_config()
    assert config.get("llm.provider") == "openrouter"
    assert (tmp_path / "config.json.broken").exists()


def test_redacted_copy(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, str(tmp_path))
    config = load_config()
    config.set("llm.groq.api_key", "secret")
    target = tmp_path / "public.json"
    config.save_public_copy(target)
    payload = json.loads(target.read_text())
    assert payload["llm"]["groq"]["api_key"] == "***redacted***"


def test_config_dir_override_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_ENV, "~/heyclicky-test")
    config = load_config()
    assert str(config.path).startswith(os.path.expanduser("~"))
