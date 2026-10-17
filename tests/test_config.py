"""Tests for Configuration and Environment Parsing."""

from pathlib import Path
from src.config import Settings


def test_default_settings(monkeypatch):
    """Test default values without environment variables."""
    # Ensure clean env for test
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)

    s = Settings()
    assert s.APP_ENV == "REDACTED"
    assert s.CANDIDATE_GRAD_YEAR == 2028
    assert s.CANDIDATE_COLLEGE == "VIT Chennai"
    assert s.DATABASE_URL.startswith("sqlite")
    assert s.UPLOAD_DIR.exists()


def test_custom_environment_settings(monkeypatch):
    """Test that environment variables cleanly override defaults."""
    monkeypatch.setenv("APP_ENV", "testing")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "99887766")
    monkeypatch.setenv("GEMINI_API_KEY", "test_gemini_key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test_token")

    s = Settings()
    assert s.is_testing() is True
    assert s.TELEGRAM_ALLOWED_USER_ID == 99887766
    assert s.GEMINI_API_KEY == "test_gemini_key"
    assert s.TELEGRAM_BOT_TOKEN == "123:test_token"
