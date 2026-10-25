"""Operational System Tables: Scheduler Audit Log and Discovery Watchlist.

Auxiliary state tables used by the background scheduler and the proactive
discovery engine. Kept separate from job models for clear domain boundaries.
"""

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SchedulerEventType(str, Enum):
    """Kinds of background events tracked in the scheduler log."""
    DAILY_DIGEST = "daily_digest"
    DEADLINE_REMINDER = "deadline_reminder"
    DISCOVERY_RUN = "discovery_run"


class SchedulerLog(SQLModel, table=True):
    """Idempotency ledger for background jobs (one-shot-per-day guarantees).

    A row means "event_type about event_key was successfully delivered on
    sent_date". Deadline reminders dedupe on (event_type, event_key=job_id,
    sent_date=today); the digest dedupes on (event_type, "digest", today).
    """
    __tablename__ = "scheduler_log"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=get_utc_now)
    event_type: SchedulerEventType = Field(index=True)
    event_key: str = Field(default="", index=True, description="Job id for reminders, 'digest' for the daily digest")
    sent_date: date = Field(index=True)
    detail: Optional[str] = Field(default=None)


class WatchTargetType(str, Enum):
    """Type of ATS board or careers page under discovery surveillance."""
    ASHBY_BOARD = "ashby_board"
    GREENHOUSE_BOARD = "greenhouse_board"
    LEVER_BOARD = "lever_board"
    CUSTOM_URL = "custom_url"


class WatchTarget(SQLModel, table=True):
    """A company ATS board / careers URL the crawler polls for new postings.

    target_key semantics per type:
    - ASHBY_BOARD / GREENHOUSE_BOARD / LEVER_BOARD: the company board slug/token
      (e.g. "linear", "stripe", "spotify")
    - CUSTOM_URL: full careers page URL to scrape
    """
    __tablename__ = "watch_targets"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=get_utc_now)
    company_name: str = Field(index=True, description="Display name recorded on ingested job rows")
    target_type: WatchTargetType
    target_key: str = Field(description="Board slug/token or full URL, depending on type")
    active: bool = Field(default=True, index=True)
    last_checked_at: Optional[datetime] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
