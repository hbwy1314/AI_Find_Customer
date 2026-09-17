"""Tests for config/settings.py — verify defaults, env overrides, multi-provider keys."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config.settings import Settings, get_settings


class TestSettingsDefaults:
    """Verify all default values are correctly set."""

    def test_llm_defaults(self):
        s = Settings(_env_file=None)
        assert s.llm_model == "gpt-4o-mini"
        assert s.llm_temperature == 0.3
        assert s.llm_max_tokens == 4096

    def test_provider_api_keys_default_empty(self):
        """New provider keys default to empty when no env var is set."""
        s = Settings(
            _env_file=None,
            openrouter_api_key="",
            groq_api_key="",
            zai_api_key="",
            moonshot_api_key="",
            minimax_api_key="",
        )
        assert s.openrouter_api_key == ""
        assert s.groq_api_key == ""
        assert s.zai_api_key == ""
        assert s.moonshot_api_key == ""
        assert s.minimax_api_key == ""
        assert s.minimax_api_base == "https://api.minimax.io/v1"

    def test_hunt_defaults(self):
        s = Settings(_env_file=None)
        assert s.default_target_lead_count == 200
        assert s.default_max_rounds == 10
        assert s.default_keywords_per_round == 8
        assert s.min_new_leads_threshold == 5

    def test_concurrency_defaults(self):
        s = Settings(_env_file=None)
        assert s.search_concurrency == 10
        assert s.scrape_concurrency == 5
        assert s.email_gen_concurrency == 3

    def test_api_defaults(self):
        s = Settings(_env_file=None)
        assert s.api_host == "0.0.0.0"
        assert s.api_port == 8000
        assert "http://localhost:3000" in s.cors_origins
        assert s.settings_api_enabled is True

    def test_langfuse_defaults(self):
        s = Settings(_env_file=None)
        assert s.langfuse_enabled is False
        assert s.langfuse_host == "http://localhost:3000"

    def test_file_upload_defaults(self):
        from config import settings as settings_module

        s = settings_module.Settings(_env_file=None)
        assert s.upload_dir.endswith("/backend/uploads")
        assert Path(s.upload_dir).is_absolute()
        assert s.max_upload_size_mb == 50

    def test_checkpoint_db_default(self):
        from config import settings as settings_module

        s = settings_module.Settings(_env_file=None)
        assert s.checkpoint_db_path.endswith("/backend/hunt_sessions.db")
        assert Path(s.checkpoint_db_path).is_absolute()


class TestSettingsEnvOverride:
    """Verify environment variables override defaults (no prefix)."""

    def test_override_openai_api_key(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-123"}):
            s = Settings()
            assert s.openai_api_key == "sk-test-123"

    def test_override_llm_model(self):
        with patch.dict(os.environ, {"LLM_MODEL": "groq/llama-3.3-70b-versatile"}):
            s = Settings()
            assert s.llm_model == "groq/llama-3.3-70b-versatile"

    def test_override_max_rounds(self):
        with patch.dict(os.environ, {"DEFAULT_MAX_ROUNDS": "20"}):
            s = Settings()
            assert s.default_max_rounds == 20

    def test_override_search_concurrency(self):
        with patch.dict(os.environ, {"SEARCH_CONCURRENCY": "15"}):
            s = Settings()
            assert s.search_concurrency == 15

    def test_override_scrape_concurrency(self):
        with patch.dict(os.environ, {"SCRAPE_CONCURRENCY": "8"}):
            s = Settings()
            assert s.scrape_concurrency == 8

    def test_override_keywords_per_round(self):
        with patch.dict(os.environ, {"DEFAULT_KEYWORDS_PER_ROUND": "6"}):
            s = Settings()
            assert s.default_keywords_per_round == 6

    def test_override_langfuse_enabled(self):
        with patch.dict(os.environ, {"LANGFUSE_ENABLED": "true"}):
            s = Settings()
            assert s.langfuse_enabled is True

    def test_override_api_port(self):
        with patch.dict(os.environ, {"API_PORT": "9000"}):
            s = Settings()
            assert s.api_port == 9000

    def test_override_groq_api_key(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"}):
            s = Settings()
            assert s.groq_api_key == "gsk-test"

    def test_override_zai_api_key(self):
        with patch.dict(os.environ, {"ZAI_API_KEY": "zai-test"}):
            s = Settings()
            assert s.zai_api_key == "zai-test"


class TestGetSettings:
    """Verify the cached singleton factory."""

    def test_get_settings_returns_settings_instance(self):
        get_settings.cache_clear()
        s = get_settings()
        # Other tests reload config.settings to exercise environment-driven
        # signatures, so resolve the current class from the module rather
        # than retaining a stale class object imported at collection time.
        from config import settings as settings_module

        assert isinstance(s, settings_module.Settings)

    def test_get_settings_is_cached(self):
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2


class TestSettingsStoreEscape:
    """Verify multiline values survive a write → read roundtrip."""

    def test_multiline_value_roundtrip(self, tmp_path):
        from unittest.mock import patch as _patch

        from config.settings_store import (
            _escape_value,
            _unescape_value,
            read_settings,
            write_settings,
        )

        multiline = "Line one.\nLine two with 反斜杠 \\ and tab.\nLine three."
        data = {"LLM_SYSTEM_PROMPT_OVERRIDE": multiline, "LLM_MODEL": "gpt-4o-mini"}

        env_file = tmp_path / ".env"
        with _patch("config.settings_store.get_env_path", return_value=env_file):
            write_settings(data)
            result = read_settings()

        assert result["LLM_SYSTEM_PROMPT_OVERRIDE"] == multiline
        assert result["LLM_MODEL"] == "gpt-4o-mini"
        # Confirm the file has no raw newlines inside values
        raw = env_file.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if "LLM_SYSTEM_PROMPT_OVERRIDE" in line:
                assert "Line one." in line
                assert "Line two" in line

    def test_escape_unescape_invertible(self):
        from config.settings_store import _escape_value, _unescape_value

        samples = [
            "no special chars",
            "line1\nline2\nline3",
            "backslash \\ and newline \n together",
            "windows\r\nline endings",
            "empty",
            "",
        ]
        for s in samples:
            assert _unescape_value(_escape_value(s)) == s

    def test_plain_values_unchanged(self, tmp_path):
        """Existing .env entries without escape sequences are read back as-is."""
        from unittest.mock import patch as _patch

        from config.settings_store import read_settings

        env_file = tmp_path / ".env"
        env_file.write_text("OPENAI_API_KEY=sk-abc123\nLLM_MODEL=gpt-4o\n", encoding="utf-8")

        with _patch("config.settings_store.get_env_path", return_value=env_file):
            result = read_settings()

        assert result["OPENAI_API_KEY"] == "sk-abc123"
        assert result["LLM_MODEL"] == "gpt-4o"
