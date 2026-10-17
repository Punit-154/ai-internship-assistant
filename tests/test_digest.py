"""Tests for Daily Internship Intelligence Digest and Reporting."""

from datetime import date, timedelta
from sqlmodel import Session
from src.database import save_job_posting
from src.models.candidate import CandidateProfile
from src.models.job import ApplicationStatus, EligibilityStatus, JobPosting
from src.services.digest import (
    build_daily_digest,
    format_digest_html,
    format_digest_text,
)


def test_build_daily_digest_empty(session: Session):
    """Verify digest handles an empty database gracefully."""
    report = build_daily_digest(session, ref_date=date(2026, 10, 17))
    assert report.report_date == date(2026, 10, 17)
    assert len(report.high_matches) == 0
    assert len(report.upcoming_deadlines) == 0
    assert len(report.active_applications) == 0
    assert len(report.action_items) > 0

    html_out = format_digest_html(report)
    assert "DAILY INTERNSHIP INTELLIGENCE BRIEFING" in html_out
    assert "No new matching roles yet" in html_out

    text_out = format_digest_text(report)
    assert "No new matching opportunities found" in text_out


def test_build_daily_digest_with_data(session: Session):
    """Verify digest correctly categorizes matches, deadlines, and active tracks."""
    ref = date(2026, 10, 17)

    # 1. High match unapplied job with upcoming deadline
    job1 = JobPosting(
        company_name="Google",
        role_title="Software Engineering Intern",
        locations=["Bangalore"],
        application_deadline=ref + timedelta(days=2),  # In 2 days!
        allowed_grad_years=[2028],
        required_skills=["Python", "FastAPI"],
        application_status=ApplicationStatus.NEW,
    )

    # 2. Applied job
    job2 = JobPosting(
        company_name="Amazon",
        role_title="SDE Intern",
        locations=["Hyderabad"],
        application_deadline=ref + timedelta(days=10),
        allowed_grad_years=[2028],
        application_status=ApplicationStatus.APPLIED,
    )

    # 3. Interviewing job
    job3 = JobPosting(
        company_name="Razorpay",
        role_title="Backend Intern",
        locations=["Bangalore"],
        allowed_grad_years=[2028],
        application_status=ApplicationStatus.INTERVIEWING,
    )

    save_job_posting(session, job1)
    save_job_posting(session, job2)
    save_job_posting(session, job3)

    profile = CandidateProfile(
        graduation_year=2028,
        summer_locations=["Bangalore", "Hyderabad"],
        verified_skills=["Python", "FastAPI"],
    )

    report = build_daily_digest(session, profile=profile, deadline_days=7, ref_date=ref)

    # Assert high match captured Google (job1 is NEW)
    assert len(report.high_matches) >= 1
    assert report.high_matches[0][0].company_name == "Google"

    # Assert upcoming deadline captured Google (due in 2 days)
    assert len(report.upcoming_deadlines) == 1
    assert report.upcoming_deadlines[0][0].company_name == "Google"
    assert report.upcoming_deadlines[0][1] == 2

    # Assert active applications captured Amazon (APPLIED) and Razorpay (INTERVIEWING)
    assert len(report.active_applications) == 2
    active_companies = {j.company_name for j in report.active_applications}
    assert "Amazon" in active_companies
    assert "Razorpay" in active_companies

    # Assert urgent action item created for 2-day deadline
    assert any("URGENT" in act and "Google" in act for act in report.action_items)

    # Assert HTML formatting renders badges and sections
    html_out = format_digest_html(report)
    assert "Google" in html_out
    assert "2d remaining" in html_out
    assert "Amazon" in html_out
    assert "APPLIED" in html_out
    assert "Razorpay" in html_out
    assert "INTERVIEWING" in html_out

    # Assert Text formatting renders
    text_out = format_digest_text(report)
    assert "Google" in text_out
    assert "2 day(s) left" in text_out
