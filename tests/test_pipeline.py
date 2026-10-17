"""Tests for Pipeline Ingestion, Verification, and Auditing."""

from unittest.mock import MagicMock, patch
from sqlmodel import Session
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    VerificationStatus,
)
from src.services.extractor import ExtractedJobData
from src.services.pipeline import (
    evaluate_batch_eligibility,
    process_text_pipeline,
    process_url_pipeline,
    verify_portal_authenticity,
)
from src.services.scraper import ScrapeResult


def test_verify_portal_authenticity_trusted_ats():
    """Verify that known ATS platforms are flagged as official."""
    is_official, status, notes = verify_portal_authenticity(
        url="https://boards.greenhouse.io/figma/jobs/12345",
        company_name="Figma",
    )
    assert is_official is True
    assert status == VerificationStatus.VERIFIED_OFFICIAL
    assert "greenhouse.io" in notes


def test_verify_portal_authenticity_company_domain():
    """Verify company matching domain."""
    is_official, status, notes = verify_portal_authenticity(
        url="https://careers.google.com/jobs/results/998877",
        company_name="Google",
    )
    assert is_official is True
    assert status == VerificationStatus.VERIFIED_OFFICIAL


def test_verify_portal_authenticity_google_forms_warning():
    """Verify that Google Forms are flagged as unverified third-party."""
    is_official, status, notes = verify_portal_authenticity(
        url="https://forms.gle/XYZ123456",
        company_name="StartupX",
    )
    assert is_official is False
    assert status == VerificationStatus.UNVERIFIED_THIRD_PARTY
    assert "Google Form" in notes


def test_evaluate_batch_eligibility():
    """Test batch evaluation logic for VIT 2028 batch."""
    # When 2028 is explicitly included
    status, reason = evaluate_batch_eligibility([2027, 2028], target_year=2028)
    assert status == EligibilityStatus.ELIGIBLE
    assert "Eligible" in reason

    # When 2028 is excluded (e.g., 2025/2026 only)
    status, reason = evaluate_batch_eligibility([2025, 2026], target_year=2028)
    assert status == EligibilityStatus.INELIGIBLE
    assert "Ineligible" in reason

    # When no batch is restricted
    status, reason = evaluate_batch_eligibility([], target_year=2028)
    assert status == EligibilityStatus.UNCERTAIN


def test_process_text_pipeline_end_to_end(session: Session):
    """Test end-to-end text ingestion and database persistence with mocked extractor."""
    mock_extracted = ExtractedJobData(
        company_name="Atlassian",
        role_title="Software Engineer Intern",
        locations=["Bengaluru"],
        application_deadline="2026-12-01",
        application_url="https://jobs.lever.co/atlassian/123",
        allowed_grad_years=[2028],
        is_internship=True,
        required_skills=["Java", "Python", "Data Structures"],
        preferred_skills=["AWS"],
        eligibility_notes="Targeting 2028 batch students",
        confidence_score=0.92,
    )

    with patch("src.services.pipeline.extract_job_from_text", return_value=mock_extracted):
        job = process_text_pipeline(
            session=session,
            raw_text="Atlassian hiring SWE intern 2028 batch in Bengaluru.",
            source_type=IngestionSource.CLI_INPUT,
        )

        assert job.id is not None
        assert job.company_name == "Atlassian"
        assert job.eligibility_status == EligibilityStatus.ELIGIBLE
        assert job.is_official_portal is True
        assert job.raw_ingestion_id is not None


def test_process_url_pipeline_end_to_end(session: Session):
    """Test end-to-end URL scraping and extraction pipeline with mocked scraper & extractor."""
    mock_scrape = ScrapeResult(
        url="https://careers.uber.com/jobs/999",
        final_url="https://careers.uber.com/jobs/999",
        success=True,
        text="Uber Software Intern Summer 2026. Batches: 2027, 2028.",
    )
    mock_extracted = ExtractedJobData(
        company_name="Uber",
        role_title="Software Engineering Intern",
        locations=["Hyderabad"],
        application_deadline=None,
        application_url="https://careers.uber.com/jobs/999",
        allowed_grad_years=[2027, 2028],
        is_internship=True,
        required_skills=["Go", "Python"],
        preferred_skills=[],
        eligibility_notes="Open to 2027 and 2028 graduates",
        confidence_score=0.88,
    )

    with patch("src.services.pipeline.scrape_url", return_value=mock_scrape):
        with patch("src.services.pipeline.extract_job_from_text", return_value=mock_extracted):
            job = process_url_pipeline(
                session=session,
                url="https://careers.uber.com/jobs/999",
                source_type=IngestionSource.CLI_INPUT,
            )

            assert job.id is not None
            assert job.company_name == "Uber"
            assert job.locations == ["Hyderabad"]
            assert job.eligibility_status == EligibilityStatus.ELIGIBLE
            assert job.is_official_portal is True
