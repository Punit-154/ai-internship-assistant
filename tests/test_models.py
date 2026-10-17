"""Tests for domain model validation, defaults, and integrity."""

import pytest
from pydantic import ValidationError
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    RawIngestion,
    VerificationStatus,
)


def test_job_posting_defaults():
    """Verify default values are set correctly without manual specification."""
    job = JobPosting(
        company_name="Atlassian",
        role_title="Software Developer Intern",
    )
    assert job.eligibility_status == EligibilityStatus.UNCERTAIN
    assert job.verification_status == VerificationStatus.UNVERIFIED_THIRD_PARTY
    assert job.application_status == ApplicationStatus.NEW
    assert job.is_official_portal is False
    assert job.locations == []
    assert job.allowed_grad_years == []
    assert job.required_skills == []


def test_raw_ingestion_creation():
    """Verify RawIngestion instantiation and initial state."""
    raw = RawIngestion(
        source_type=IngestionSource.TELEGRAM_IMAGE,
        media_path="data/uploads/screenshot_01.png",
    )
    assert raw.source_type == IngestionSource.TELEGRAM_IMAGE
    assert raw.media_path == "data/uploads/screenshot_01.png"
    assert raw.processed is False
    assert raw.error_message is None
