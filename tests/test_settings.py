"""Tests for Settings — API key resolution, TOML load/save, defaults."""
from __future__ import annotations

import pytest

from ofp_playground.config.settings import (
    ApiKeysConfig,
    DefaultsConfig,
    FloorConfig,
    Settings,
)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_settings_creates_with_defaults(self):
        s = Settings()
        assert s.floor is not None
        assert s.api_keys is not None
        assert s.defaults is not None

    def test_floor_default_policy(self):
        assert FloorConfig().policy == "sequential"

    def test_floor_default_max_agents(self):
        assert FloorConfig().max_agents == 10

    def test_floor_default_timeout(self):
        assert FloorConfig().timeout_seconds == 30

    def test_api_keys_all_none_initially(self):
        ak = ApiKeysConfig()
        assert ak.anthropic is None
        assert ak.openai is None
        assert ak.google is None
        assert ak.huggingface is None

    def test_defaults_models_non_empty(self):
        d = DefaultsConfig()
        assert d.llm_model_anthropic
        assert d.llm_model_openai
        assert d.llm_model_google
        assert d.llm_model_huggingface


# ---------------------------------------------------------------------------
# get_*_key — env var resolution
# ---------------------------------------------------------------------------

class TestApiKeyResolution:
    def test_anthropic_from_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        s = Settings()
        assert s.get_anthropic_key() == "sk-ant-test"

    def test_anthropic_from_config_overrides_env(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
        s = Settings()
        s.api_keys.anthropic = "from-config"
        assert s.get_anthropic_key() == "from-config"

    def test_anthropic_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        s = Settings()
        assert s.get_anthropic_key() is None

    def test_openai_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai")
        s = Settings()
        assert s.get_openai_key() == "sk-test-openai"

    def test_openai_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        s = Settings()
        assert s.get_openai_key() is None

    def test_google_from_env(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
        s = Settings()
        assert s.get_google_key() == "AIza-test"

    def test_huggingface_from_hf_api_key(self, monkeypatch):
        monkeypatch.setenv("HF_API_KEY", "hf_test")
        monkeypatch.delenv("HF_TOKEN", raising=False)
        s = Settings()
        assert s.get_huggingface_key() == "hf_test"

    def test_huggingface_from_hf_token_fallback(self, monkeypatch):
        monkeypatch.delenv("HF_API_KEY", raising=False)
        monkeypatch.setenv("HF_TOKEN", "hf_token_fallback")
        s = Settings()
        assert s.get_huggingface_key() == "hf_token_fallback"

    def test_huggingface_config_takes_priority_over_env(self, monkeypatch):
        monkeypatch.setenv("HF_API_KEY", "from-env")
        s = Settings()
        s.api_keys.huggingface = "from-config"
        assert s.get_huggingface_key() == "from-config"

    def test_huggingface_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("HF_API_KEY", raising=False)
        monkeypatch.delenv("HF_TOKEN", raising=False)
        s = Settings()
        assert s.get_huggingface_key() is None


# ---------------------------------------------------------------------------
# Settings.load — from file
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_missing_file_returns_defaults(self, tmp_path, monkeypatch):
        missing = tmp_path / "config.toml"
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", missing)
        s = Settings.load()
        assert s.floor.policy == "sequential"
        assert s.api_keys.anthropic is None

    def test_load_reads_floor_section(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[floor]\npolicy = "round_robin"\nmax_agents = 5\ntimeout_seconds = 60\n'
        )
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s = Settings.load()
        assert s.floor.policy == "round_robin"
        assert s.floor.max_agents == 5
        assert s.floor.timeout_seconds == 60

    def test_load_reads_api_keys_section(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(
            b'[api_keys]\nanthropic = "sk-ant-from-file"\nopenai = "sk-openai-from-file"\n'
        )
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s = Settings.load()
        assert s.api_keys.anthropic == "sk-ant-from-file"
        assert s.api_keys.openai == "sk-openai-from-file"

    def test_load_reads_defaults_section(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(
            b'[defaults]\nrelevance_filter = false\n'
        )
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s = Settings.load()
        assert s.defaults.relevance_filter is False

    def test_load_ignores_unknown_keys(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        config_file.write_bytes(b'[floor]\nunknown_key = "value"\n')
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s = Settings.load()  # should not raise
        assert s.floor.policy == "sequential"  # default unchanged


# ---------------------------------------------------------------------------
# Settings.save
# ---------------------------------------------------------------------------

class TestSave:
    def test_save_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", tmp_path / "config.toml")
        s = Settings()
        s.save()
        assert (tmp_path / "config.toml").exists()

    def test_save_roundtrip(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)

        s = Settings()
        s.floor.policy = "moderated"
        s.floor.max_agents = 7
        s.api_keys.anthropic = "sk-ant-saved"
        s.defaults.llm_model_huggingface = "example/custom-model"
        s.save()

        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s2 = Settings.load()
        assert s2.floor.policy == "moderated"
        assert s2.floor.max_agents == 7
        assert s2.api_keys.anthropic == "sk-ant-saved"
        assert s2.defaults.llm_model_huggingface == "example/custom-model"

    def test_save_omits_none_api_keys(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.toml"
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("ofp_playground.config.settings.CONFIG_FILE", config_file)
        s = Settings()
        s.api_keys.anthropic = None  # explicitly None
        s.save()
        import tomllib
        with open(config_file, "rb") as f:
            data = tomllib.load(f)
        # The [api_keys] section should have no 'anthropic' key when it's None
        assert "anthropic" not in data.get("api_keys", {})
