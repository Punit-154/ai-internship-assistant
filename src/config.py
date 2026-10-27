"""Application Configuration & Secrets Management.

Uses Pydantic Settings to load configuration from environment variables
or a local `.env` file, ensuring strict type-safety and early validation.
"""

from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings schema validated at runtime."""

    # Project paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    UPLOAD_DIR: Path = Path(__file__).resolve().parent.parent / "data" / "uploads"

    # Environment
    APP_ENV: str = Field(default="development", description="Runtime environment: development, testing, production")

    # Database
    DATABASE_URL: str = Field(
        default="sqlite:///internships.db",
        description="SQLAlchemy database connection URI (defaults to SQLite)"
    )

    # API Keys & Secrets
    GEMINI_API_KEY: Optional[str] = Field(
        default=None,
        description="Google AI Studio Gemini API Key"
    )
    GEMINI_MODEL: str = Field(
        default="gemini-3.5-flash",
        description="Gemini Model Identifier"
    )
    TELEGRAM_BOT_TOKEN: Optional[str] = Field(
        default=None,
        description="Telegram Bot API Token issued by @BotFather"
    )
    TELEGRAM_ALLOWED_USER_ID: Optional[int] = Field(
        default=None,
        description="Numeric Telegram user ID whitelisted to access the bot"
    )

    # User Profile Default Constraints (VIT Chennai, 2028 batch)
    CANDIDATE_GRAD_YEAR: int = Field(default=2028, description="Target graduation batch")
    CANDIDATE_COLLEGE: str = Field(default="VIT Chennai", description="College name")

    # Background Scheduler (Telegram JobQueue)
    SCHEDULER_ENABLED: bool = Field(
        default=True,
        description="Enable scheduled digest, deadline reminders, and discovery checks",
    )
    DIGEST_HOUR_LOCAL: int = Field(
        default=8,
        description="Hour of day (in local timezone) to send the daily intelligence digest",
    )
    TZ_OFFSET_HOURS: float = Field(
        default=5.5,
        description="Local timezone offset from UTC in hours (IST = +5.5)",
    )
    DEADLINE_REMINDER_WINDOW_DAYS: int = Field(
        default=2,
        description="Remind about unapplied opportunities closing within N days",
    )
    DEADLINE_CHECK_INTERVAL_HOURS: int = Field(
        default=4,
        description="How often the deadline reminder sweep runs",
    )
    DISCOVERY_ENABLED: bool = Field(
        default=True,
        description="Enable scheduled proactive job discovery from ATS board watchlists",
    )
    DISCOVERY_CHECK_INTERVAL_HOURS: int = Field(
        default=2,
        description="How often the ATS board watchlist crawler runs",
    )
    DISCOVERY_ALERT_MIN_SCORE: float = Field(
        default=0.65,
        description="Minimum overall match score for a discovered job to trigger a Telegram alert",
    )
    DISCOVERY_MAX_PER_RUN: int = Field(
        default=8,
        description="Maximum new jobs ingested per watch target per discovery run",
    )

    # Headless browser rendering (optional Playwright backend)
    PLAYWRIGHT_ENABLED: bool = Field(
        default=True,
        description="Use a locally installed headless Chromium (optional playwright package) to render JavaScript-heavy job pages when static scraping returns an empty shell",
    )

    # Contact enrichment (Prospeo) — optional; system degrades to manual email entry
    PROSPEO_API_KEY: Optional[str] = Field(
        default=None,
        description="Prospeo API key for verified work-email enrichment (Starter plan required for API access)"
    )

    # GitHub API (optional personal token raises the 60 req/h unauthenticated limit)
    GITHUB_TOKEN: Optional[str] = Field(
        default=None,
        description="GitHub personal access token used by the alumni/engineer finder"
    )

    # Approved cold-outreach delivery (Gmail SMTP with an app password)
    GMAIL_ADDRESS: Optional[str] = Field(
        default=None,
        description="Gmail address used to send approved cold-outreach emails"
    )
    GMAIL_APP_PASSWORD: Optional[str] = Field(
        default=None,
        description="Gmail App Password (NOT the account password) for SMTP delivery"
    )

    # Outreach rate caps (spam-safety design)
    OUTREACH_SEND_ENABLED: bool = Field(
        default=True,
        description="Master kill-switch: set false to block ALL outbound email sends, even approved ones"
    )
    OUTREACH_MAX_FIRST_SENDS_PER_DAY: int = Field(
        default=20,
        description="Maximum first-contact cold emails approved per day"
    )
    OUTREACH_MAX_FOLLOWUPS_PER_DAY: int = Field(
        default=10,
        description="Maximum follow-up emails approved per day"
    )
    OUTREACH_FOLLOWUP_DAYS: int = Field(
        default=7,
        description="Days to wait after a cold email before scheduling the follow-up"
    )
    OUTREACH_CHECK_INTERVAL_HOURS: int = Field(
        default=6,
        description="How often (hours) the scheduler sweeps for due outreach follow-ups"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("UPLOAD_DIR")
    @classmethod
    def ensure_upload_dir_exists(cls, v: Path) -> Path:
        """Ensure that the media upload directory exists on disk."""
        v.mkdir(parents=True, exist_ok=True)
        return v

    def is_production(self) -> bool:
        return self.APP_ENV.lower() == "production"

    def is_testing(self) -> bool:
        return self.APP_ENV.lower() == "testing"


# Singleton instance for import throughout application
settings = Settings()
