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
    APP_ENV: str = Field(default="REDACTED", description="Runtime environment: REDACTED, testing, production")

    # Database
    DATABASE_URL: str = Field(
        default="REDACTED",
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
