"""Tests for Database Operations, Deduplication, and Repositories."""

from datetime import date
from sqlmodel import Session, select
from src.database import (
    find_existing_job,
    get_job_by_id,
    list_jobs,
    purge_duplicate_jobs,
    save_job_posting,
    save_or_update_job_posting,
    save_raw_ingestion,
    update_job_application_status,
)
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    VerificationStatus,
)


def test_save_raw_ingestion(session: Session):
    raw = save_raw_ingestion(
        session=session,
        source_type=IngestionSource.TELEGRAM_URL,
        raw_content="https://careers.google.com/jobs/results/123",
    )
    assert raw.id is not None
    assert raw.source_type == IngestionSource.TELEGRAM_URL
    assert raw.processed is False


def test_save_and_retrieve_job_posting(session: Session):
    job = JobPosting(
        company_name="Google",
        role_title="Software Engineering Intern, Summer 2026",
        locations=["Bangalore", "Hyderabad"],
        application_deadline=date(2026, 12, 31),
        application_url="https://careers.google.com/jobs/123",
        allowed_grad_years=[2027, 2028],
        eligibility_status=EligibilityStatus.ELIGIBLE,
        eligibility_reason="Accepts 2028 graduates",
        required_skills=["Python", "Data Structures", "Algorithms"],
        preferred_skills=["Cloud", "FastAPI"],
        verification_status=VerificationStatus.VERIFIED_OFFICIAL,
        verification_source="careers.google.com",
        is_official_portal=True,
    )

    saved = save_job_posting(session, job)
    assert saved.id is not None

    retrieved = get_job_by_id(session, saved.id)
    assert retrieved is not None
    assert retrieved.company_name == "Google"
    assert retrieved.role_title == "Software Engineering Intern, Summer 2026"
    assert retrieved.locations == ["Bangalore", "Hyderabad"]


def test_deduplication_same_company_same_role(session: Session):
    """Verify submitting the same company and role merges instead of duplicating."""
    job1 = JobPosting(
        company_name="Kaleris",
        role_title="Associate Consultant Intern",
        locations=["Chennai"],
        application_url=None,
    )
    saved1, is_new1 = save_or_update_job_posting(session, job1)
    assert is_new1 is True
    initial_id = saved1.id

    # Second submission adds an application URL
    job2 = JobPosting(
        company_name="Kaleris",
        role_title="Associate Consultant Intern",
        locations=["Chennai", "Bangalore"],
        application_url="https://kaleris.com/jobs/123",
    )
    saved2, is_new2 = save_or_update_job_posting(session, job2)
    assert is_new2 is False
    assert saved2.id == initial_id  # Reused same ID!
    assert saved2.application_url == "https://kaleris.com/jobs/123"
    assert "Bangalore" in saved2.locations

    # Total records in table must be exactly 1
    all_jobs = session.exec(select(JobPosting)).all()
    assert len(all_jobs) == 1


def test_same_company_different_role_allowed(session: Session):
    """Verify that different roles at the same company are both preserved."""
    job1 = JobPosting(
        company_name="Google",
        role_title="Software Engineering Intern",
    )
    job2 = JobPosting(
        company_name="Google",
        role_title="Cloud Technical Resident Intern",
    )
    save_job_posting(session, job1)
    save_job_posting(session, job2)

    all_jobs = session.exec(select(JobPosting)).all()
    assert len(all_jobs) == 2


def test_list_and_filter_jobs(session: Session):
    job1 = JobPosting(
        company_name="Amazon",
        role_title="SDE Intern",
        application_status=ApplicationStatus.NEW,
    )
    job2 = JobPosting(
        company_name="Microsoft",
        role_title="SWE Intern",
        application_status=ApplicationStatus.APPLIED,
    )
    save_job_posting(session, job1)
    save_job_posting(session, job2)

    all_jobs = list_jobs(session)
    assert len(all_jobs) == 2

    applied_jobs = list_jobs(session, status=ApplicationStatus.APPLIED)
    assert len(applied_jobs) == 1
    assert applied_jobs[0].company_name == "Microsoft"


def test_update_job_status(session: Session):
    job = JobPosting(
        company_name="Razorpay",
        role_title="Backend Engineering Intern",
        application_status=ApplicationStatus.NEW,
    )
    saved = save_job_posting(session, job)

    updated = update_job_application_status(session, saved.id, ApplicationStatus.APPLIED)
    assert updated is not None
    assert updated.application_status == ApplicationStatus.APPLIED
