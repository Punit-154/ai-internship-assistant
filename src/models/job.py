"""Database and Domain Models for Internship Ingestion and Tracking.

Uses SQLModel (SQLAlchemy 2.0 + Pydantic v2) for unified database tables
and schema validation.
"""

from datetime import date, datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlmodel import JSON, Field, SQLModel


def get_utc_now() -> datetime:
    """Return timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


class IngestionSource(str, Enum):
    """Source medium through which an opportunity entered the system."""
    TELEGRAM_URL = "telegram_url"
    TELEGRAM_IMAGE = "telegram_image"
    TELEGRAM_TEXT = "telegram_text"
    CLI_INPUT = "cli_input"
    MANUAL_ENTRY = "manual_entry"
    DISCOVERY = "discovery"


class EligibilityStatus(str, Enum):
    """Eligibility verdict against candidate's profile (VIT 2028)."""
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNCERTAIN = "uncertain"


class VerificationStatus(str, Enum):
    """Integrity and official source verification state."""
    VERIFIED_OFFICIAL = "verified_official"        # Found on official domain/ATS (e.g. Greenhouse, Workday)
    UNVERIFIED_THIRD_PARTY = "unverified_third_party"  # Unverified link, Google form, or social claim
    EXPIRED_OR_DEAD = "expired_or_dead"            # 404, closed portal, or passed deadline
    SUSPICIOUS = "suspicious"                      # Known spam, fee-for-internship scam


class ApplicationStatus(str, Enum):
    """Personal lifecycle tracking of the opportunity."""
    NEW = "new"
    REVIEWED = "reviewed"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class RawIngestion(SQLModel, table=True):
    """Audit log of unparsed input payloads sent from phone or CLI.
    
    Guarantees raw data is never lost even if the LLM extraction fails.
    """
    __tablename__ = "raw_ingestions"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=get_utc_now)
    source_type: IngestionSource
    raw_content: Optional[str] = Field(default=None, description="Raw text, forwarded message, or URL")
    media_path: Optional[str] = Field(default=None, description="Local path to uploaded screenshot or PDF")
    processed: bool = Field(default=False)
    error_message: Optional[str] = Field(default=None)


class JobPosting(SQLModel, table=True):
    """Structured and normalized internship posting record."""
    __tablename__ = "job_postings"

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=get_utc_now)
    updated_at: datetime = Field(default_factory=get_utc_now)

    # Reference to original raw ingestion
    raw_ingestion_id: Optional[int] = Field(default=None, foreign_key="raw_ingestions.id")

    # Core job details
    company_name: str = Field(index=True)
    role_title: str = Field(index=True)
    locations: List[str] = Field(default_factory=list, sa_type=JSON)
    application_deadline: Optional[date] = Field(default=None)
    application_url: Optional[str] = Field(default=None)

    # Eligibility & Grad Year Constraints (Target: 2028)
    allowed_grad_years: List[int] = Field(default_factory=list, sa_type=JSON)
    eligibility_status: EligibilityStatus = Field(default=EligibilityStatus.UNCERTAIN)
    eligibility_reason: str = Field(default="Not yet evaluated")

    # Skills & Requirements
    required_skills: List[str] = Field(default_factory=list, sa_type=JSON)
    preferred_skills: List[str] = Field(default_factory=list, sa_type=JSON)

    # Trust & Verification
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED_THIRD_PARTY)
    verification_source: Optional[str] = Field(default=None, description="Official careers domain or ATS name")
    is_official_portal: bool = Field(default=False)
    verification_notes: Optional[str] = Field(default=None)

    # Workflow & Tracking
    application_status: ApplicationStatus = Field(default=ApplicationStatus.NEW, index=True)
    notes: Optional[str] = Field(default=None)
