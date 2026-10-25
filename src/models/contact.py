"""Contact & Networking Domain Models (P1 Outbound Outreach).

Tracks verified recruiters / engineers / alumni discovered or enriched
manually, feeding the approved cold-outreach pipeline.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ContactSource(str, Enum):
    """How the contact entered the tracked network."""
    MANUAL = "manual"
    PROSPEO = "prospeo"
    GITHUB_PUBLIC = "github_public"


class EmailStatus(str, Enum):
    """Deliverability state of the stored email address."""
    UNVERIFIED = "unverified"
    PROSPEO_VERIFIED = "prospeo_verified"
    BOUNCED = "bounced"
    REPLIED = "replied"


class OutreachStatus(str, Enum):
    """Cold-outreach lifecycle for this contact."""
    NONE = "none"
    DRAFTED = "drafted"
    SENT = "sent"
    FOLLOWED_UP = "followed_up"
    REPLIED = "replied"


class Contact(SQLModel, table=True):
    """A tracked recruiter / engineer / alumni contact for networking outreach."""
    __tablename__ = "contacts"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    # Identity
    full_name: str = Field(index=True)
    company_name: Optional[str] = Field(default=None, index=True)
    role_title: Optional[str] = Field(default=None)
    relationship: str = Field(default="", description="alumni | recruiter | engineer | hiring_manager | ...")

    # Reach channels
    email: Optional[str] = Field(default=None, index=True)
    linkedin_url: Optional[str] = Field(default=None)
    github_url: Optional[str] = Field(default=None)

    # Provenance & deliverability
    source: ContactSource = Field(default=ContactSource.MANUAL)
    email_status: EmailStatus = Field(default=EmailStatus.UNVERIFIED)

    # Outreach lifecycle
    outreach_status: OutreachStatus = Field(default=OutreachStatus.NONE, index=True)
    sent_at: Optional[datetime] = Field(default=None)
    followup_at: Optional[str] = Field(default=None, description="ISO date the next follow-up is due")
    replied_at: Optional[datetime] = Field(default=None)
    last_job_id: Optional[int] = Field(default=None, description="Most recent job this contact was drafted against")

    notes: Optional[str] = Field(default=None)
