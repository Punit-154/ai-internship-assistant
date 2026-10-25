"""Tests for Database Operations, Deduplication, and Repositories."""

from datetime import date, timedelta

from sqlmodel import Session, select

from src.database import (
    add_watch_target,
    deactivate_watch_target,
    get_job_by_id,
    get_jobs_needing_deadline_reminder,
    has_scheduler_event,
    list_jobs,
    list_watch_targets,
    log_scheduler_event,
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
from src.models.system import SchedulerEventType, SchedulerLog


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


# ==============================================================================
# Scheduler Log & Deadline Reminders
# ==============================================================================

def test_scheduler_log_idempotency(session: Session):
    today = date.today()
    assert has_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today) is False

    log_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today)
    assert has_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today) is True

    log_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today)
    logs = session.exec(select(SchedulerLog)).all()
    assert len(logs) == 1

    assert has_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today + timedelta(days=1)) is False


def test_deadline_reminder_window_filters_and_dedupes(session: Session):
    today = date.today()
    urgent = JobPosting(
        company_name="Stripe",
        role_title="Machine Learning Intern",
        application_deadline=today + timedelta(days=2),
        application_status=ApplicationStatus.NEW,
    )
    applied = JobPosting(
        company_name="Zoho",
        role_title="SDE Intern",
        application_deadline=today + timedelta(days=1),
        application_status=ApplicationStatus.APPLIED,
    )
    far = JobPosting(
        company_name="Google",
        role_title="STEP Intern",
        application_deadline=today + timedelta(days=10),
        application_status=ApplicationStatus.NEW,
    )
    past = JobPosting(
        company_name="Swiggy",
        role_title="Backend Intern",
        application_deadline=today - timedelta(days=1),
        application_status=ApplicationStatus.NEW,
    )
    for j in (urgent, applied, far, past):
        save_job_posting(session, j)

    results = get_jobs_needing_deadline_reminder(session, window_days=2, ref_date=today)
    ids = [j.id for j, _ in results]
    assert urgent.id in ids
    assert applied.id not in ids
    assert far.id not in ids
    assert past.id not in ids

    log_scheduler_event(session, SchedulerEventType.DEADLINE_REMINDER, str(urgent.id), today)
    results_after = get_jobs_needing_deadline_reminder(session, window_days=2, ref_date=today)
    assert urgent.id not in [j.id for j, _ in results_after]


# ==============================================================================
# Watch Targets
# ==============================================================================

def test_add_and_list_watch_targets(session: Session):
    t1 = add_watch_target(session, "Linear", "ashby", "linear")
    assert t1.id is not None
    assert t1.target_type.value == "ashby_board"
    assert t1.active is True

    add_watch_target(session, "Spotify", "lever", "spotify")
    targets = list_watch_targets(session)
    assert len(targets) == 2

    deactivate_watch_target(session, t1.id)
    assert len(list_watch_targets(session)) == 1
    assert len(list_watch_targets(session, active_only=False)) == 2


def test_add_watch_target_invalid_type_raises(session: Session):
    import pytest
    with pytest.raises(ValueError, match="Invalid watch type"):
        add_watch_target(session, "X", "bamboohr", "x")
    with pytest.raises(ValueError, match="Company name is required"):
        add_watch_target(session, "  ", "lever", "x")
