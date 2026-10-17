"""Tests for Application Status Tracking and Deadline Queries."""

from datetime import date, timedelta
import pytest
from sqlmodel import Session
from src.database import (
    get_expired_deadlines,
    get_jobs_by_status,
    get_upcoming_deadlines,
    save_job_posting,
    update_job_application_status,
)
from src.models.job import ApplicationStatus, JobPosting


def test_update_status_string_conversion(session: Session):
    """Verify string status values are parsed correctly case-insensitively."""
    job = JobPosting(company_name="Stripe", role_title="Backend Intern")
    saved = save_job_posting(session, job)

    updated = update_job_application_status(session, saved.id, "applied", notes="Applied via referral")
    assert updated is not None
    assert updated.application_status == ApplicationStatus.APPLIED
    assert updated.notes == "Applied via referral"

    updated2 = update_job_application_status(session, saved.id, "INTERVIEWING")
    assert updated2.application_status == ApplicationStatus.INTERVIEWING


def test_update_status_invalid_raises_error(session: Session):
    """Verify invalid status string raises ValueError with helpful message."""
    job = JobPosting(company_name="Netflix", role_title="SWE Intern")
    saved = save_job_posting(session, job)

    with pytest.raises(ValueError, match="Invalid status 'pending'"):
        update_job_application_status(session, saved.id, "pending")


def test_get_upcoming_deadlines_filtering(session: Session):
    """Verify upcoming deadlines excludes archived/rejected and only captures within window."""
    today = date(2026, 10, 17)

    # Within window (3 days away) - active
    j1 = JobPosting(
        company_name="Uber",
        role_title="SDE Intern",
        application_deadline=today + timedelta(days=3),
        application_status=ApplicationStatus.NEW,
    )
    # Within window (5 days away) - ARCHIVED (should be excluded)
    j2 = JobPosting(
        company_name="Meta",
        role_title="Research Intern",
        application_deadline=today + timedelta(days=5),
        application_status=ApplicationStatus.ARCHIVED,
    )
    # Outside window (12 days away)
    j3 = JobPosting(
        company_name="Apple",
        role_title="iOS Intern",
        application_deadline=today + timedelta(days=12),
        application_status=ApplicationStatus.NEW,
    )

    save_job_posting(session, j1)
    save_job_posting(session, j2)
    save_job_posting(session, j3)

    upcoming_7d = get_upcoming_deadlines(session, days_ahead=7, ref_date=today)
    assert len(upcoming_7d) == 1
    assert upcoming_7d[0].company_name == "Uber"

    upcoming_14d = get_upcoming_deadlines(session, days_ahead=14, ref_date=today)
    assert len(upcoming_14d) == 2
    comp_names = [j.company_name for j in upcoming_14d]
    assert "Uber" in comp_names
    assert "Apple" in comp_names
    assert "Meta" not in comp_names  # Excluded because archived


def test_get_expired_deadlines(session: Session):
    """Verify past deadlines are accurately identified for unapplied jobs."""
    today = date(2026, 10, 17)

    # Expired and NEW
    j1 = JobPosting(
        company_name="OldCorp",
        role_title="Intern",
        application_deadline=today - timedelta(days=2),
        application_status=ApplicationStatus.NEW,
    )
    # Expired but already APPLIED (should be excluded from expired unapplied)
    j2 = JobPosting(
        company_name="AppliedCorp",
        role_title="Intern",
        application_deadline=today - timedelta(days=2),
        application_status=ApplicationStatus.APPLIED,
    )

    save_job_posting(session, j1)
    save_job_posting(session, j2)

    expired = get_expired_deadlines(session, ref_date=today)
    assert len(expired) == 1
    assert expired[0].company_name == "OldCorp"


def test_get_jobs_by_status(session: Session):
    """Verify multiple status querying works properly."""
    j1 = JobPosting(company_name="A", role_title="R", application_status=ApplicationStatus.APPLIED)
    j2 = JobPosting(company_name="B", role_title="R", application_status=ApplicationStatus.INTERVIEWING)
    j3 = JobPosting(company_name="C", role_title="R", application_status=ApplicationStatus.REJECTED)

    save_job_posting(session, j1)
    save_job_posting(session, j2)
    save_job_posting(session, j3)

    active = get_jobs_by_status(session, statuses=[ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEWING])
    assert len(active) == 2
    active_companies = {j.company_name for j in active}
    assert active_companies == {"A", "B"}
