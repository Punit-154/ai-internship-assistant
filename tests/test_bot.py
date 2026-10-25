"""Tests for Telegram Bot Ingestion Adapter (Mocked & Offline)."""

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from src.config import settings
from src.interfaces.bot import (
    URL_REGEX,
    build_telegram_app,
    format_job_html_card,
    get_job_inline_keyboard,
    is_authorized,
)
from src.models.job import (
    EligibilityStatus,
    JobPosting,
    VerificationStatus,
)


def test_is_authorized(monkeypatch):
    """Verify authorization filter allows only whitelisted user ID."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_ID", 123456789)

    assert is_authorized(123456789) is True
    assert is_authorized(987654321) is False


def test_is_authorized_no_whitelist_set(monkeypatch):
    """Verify when whitelist is unset, access is denied by default (private bot)."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_ID", None)
    assert is_authorized(99999) is False


def test_is_authorized_unknown_user_or_none(monkeypatch):
    """Verify unknown senders (channels, anonymous admins) are always denied."""
    monkeypatch.setattr(settings, "TELEGRAM_ALLOWED_USER_ID", 123456789)
    assert is_authorized(None) is False
    assert is_authorized(0) is False


def test_format_job_html_card():
    """Verify HTML card formatting escapes characters and renders badges correctly."""
    job = JobPosting(
        id=42,
        company_name="AT&T <Communications>",
        role_title="Backend & Cloud Intern",
        locations=["Bangalore"],
        allowed_grad_years=[2028],
        eligibility_status=EligibilityStatus.ELIGIBLE,
        eligibility_reason="Accepts 2028 graduates",
        is_official_portal=True,
        verification_status=VerificationStatus.VERIFIED_OFFICIAL,
        application_url="https://att.jobs/123",
        required_skills=["Python", "C++"],
        notes="Match Score: 95%",
    )

    card = format_job_html_card(job)
    assert "JOB RECORD #42" in card
    # Assert HTML escaping
    assert "&lt;Communications&gt;" in card
    assert "Backend &amp; Cloud Intern" in card
    # Assert badges
    assert "ELIGIBLE (2028 Batch)" in card
    assert "Official Careers Portal" in card
    assert "https://att.jobs/123" in card


def test_url_regex_matcher():
    """Verify regex correctly detects URLs in social media forwards."""
    text1 = "Hey check this internship: https://boards.greenhouse.io/figma/jobs/123 apply soon!"
    match1 = URL_REGEX.search(text1)
    assert match1 is not None
    assert match1.group(0) == "https://boards.greenhouse.io/figma/jobs/123"

    text2 = "Just an announcement without any link"
    assert URL_REGEX.search(text2) is None


def test_build_telegram_app_missing_token(monkeypatch):
    """Verify exception raised when TELEGRAM_BOT_TOKEN is placeholder."""
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456789:ABCplaceholder")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN is missing or placeholder"):
        build_telegram_app()


def test_get_job_inline_keyboard():
    """Verify inline keyboard generates buttons with appropriate callback signatures."""
    markup = get_job_inline_keyboard(42)
    assert len(markup.inline_keyboard) == 2
    row1 = markup.inline_keyboard[0]
    row2 = markup.inline_keyboard[1]

    assert row1[0].text == "📄 Tailor Resume"
    assert row1[0].callback_data == "tailor:42"
    assert row1[1].text == "✅ Applied"
    assert row1[1].callback_data == "status:42:applied"

    assert row2[0].text == "🗣️ Interviewing"
    assert row2[0].callback_data == "status:42:interviewing"
    assert row2[1].text == "📦 Archive"
    assert row2[1].callback_data == "status:42:archived"

    # With direct application URL
    markup_with_url = get_job_inline_keyboard(42, application_url="https://razorpay.com/careers/intern")
    assert len(markup_with_url.inline_keyboard) == 3
    assert markup_with_url.inline_keyboard[0][0].text == "🔗 Apply Directly"
    assert markup_with_url.inline_keyboard[0][0].url == "https://razorpay.com/careers/intern"


def test_build_telegram_app_registers_milestone7_handlers(monkeypatch):
    """Verify status, deadlines, digest and callback handlers are properly registered."""
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456789:AABBCCTestValidTokenString12345")
    app = build_telegram_app()

    command_handlers = []
    has_callback_handler = False

    for group in app.handlers.values():
        for handler in group:
            if isinstance(handler, CommandHandler):
                command_handlers.extend(list(handler.commands))
            elif isinstance(handler, CallbackQueryHandler):
                has_callback_handler = True

    assert "status" in command_handlers
    assert "deadlines" in command_handlers
    assert "digest" in command_handlers
    assert "tailor" in command_handlers
    assert "watch" in command_handlers
    assert "discover" in command_handlers
    assert has_callback_handler is True


def test_background_scheduler_registers_jobs(monkeypatch):
    """Tier 1: verify the 8AM digest, deadline sweep, and discovery sweep are armed."""
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456789:AABBCCTestValidTokenString12345")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", True)
    app = build_telegram_app()

    job_names = [job.name for job in app.job_queue.jobs()]
    assert "daily_digest" in job_names
    assert "deadline_sweep" in job_names
    assert "discovery_sweep" in job_names


def test_scheduler_disabled_leaves_no_jobs(monkeypatch):
    """Verify SCHEDULER_ENABLED=false registers zero background jobs."""
    monkeypatch.setattr(settings, "APP_ENV", "testing")
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "123456789:AABBCCTestValidTokenString12345")
    monkeypatch.setattr(settings, "SCHEDULER_ENABLED", False)
    app = build_telegram_app()

    assert len(app.job_queue.jobs()) == 0
