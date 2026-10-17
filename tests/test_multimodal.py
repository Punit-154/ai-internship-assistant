"""Tests for Multimodal Screenshot & Image OCR Pipeline."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from sqlmodel import Session
from src.models.job import EligibilityStatus, IngestionSource, VerificationStatus
from src.services.extractor import ExtractedJobData, extract_job_from_image
from src.services.pipeline import process_image_pipeline


# Minimal valid 1x1 PNG byte sequence
TINY_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00"
    b"\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_extract_job_from_image_mocked():
    """Verify multimodal extraction from image bytes with a mocked GenAI client."""
    mock_payload = {
        "company_name": "Razorpay",
        "role_title": "Backend Engineering Intern",
        "locations": ["Bangalore"],
        "application_deadline": "2026-11-20",
        "application_url": "https://razorpay.com/careers/backend-intern",
        "allowed_grad_years": [2027, 2028],
        "is_internship": True,
        "required_skills": ["Python", "FastAPI", "SQL"],
        "preferred_skills": ["Docker", "AWS"],
        "eligibility_notes": "Instagram hiring poster targeting pre-final year students",
        "confidence_score": 0.95,
    }

    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_payload)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    result = extract_job_from_image(
        image_bytes=TINY_PNG_BYTES,
        mime_type="image/png",
        client=mock_client,
    )

    assert result.company_name == "Razorpay"
    assert result.role_title == "Backend Engineering Intern"
    assert result.allowed_grad_years == [2027, 2028]
    assert "FastAPI" in result.required_skills


def test_extract_job_from_image_empty_error():
    """Verify passing empty image bytes raises ValueError."""
    with pytest.raises(ValueError, match="Image data is empty or corrupt"):
        extract_job_from_image(image_bytes=b"", mime_type="image/png")


def test_process_image_pipeline_missing_file(session: Session):
    """Verify FileNotFoundError is raised when image path does not exist."""
    with pytest.raises(FileNotFoundError, match="Media file not found"):
        process_image_pipeline(session=session, file_path=Path("non_existent_image.png"))


def test_process_image_pipeline_unsupported_format(session: Session, tmp_path: Path):
    """Verify ValueError is raised when media format is unsupported."""
    bad_file = tmp_path / "script.exe"
    bad_file.write_bytes(b"MZfakeexecutablebinary")

    with pytest.raises(ValueError, match="Unsupported file format"):
        process_image_pipeline(session=session, file_path=bad_file)


def test_process_image_pipeline_end_to_end(session: Session, tmp_path: Path):
    """Test full end-to-end ingestion of an image file with persistence and audit trail."""
    test_img = tmp_path / "instagram_hiring_post.png"
    test_img.write_bytes(TINY_PNG_BYTES)

    mock_extracted = ExtractedJobData(
        company_name="Razorpay",
        role_title="Backend Engineering Intern",
        locations=["Bangalore"],
        application_deadline="2026-11-20",
        application_url="https://razorpay.com/careers/backend-intern",
        allowed_grad_years=[2028],
        is_internship=True,
        required_skills=["Python", "FastAPI", "SQL"],
        preferred_skills=["Docker"],
        eligibility_notes="Visual Flyer for 2028 batch",
        confidence_score=0.94,
    )

    with patch("src.services.pipeline.extract_job_from_image", return_value=mock_extracted):
        job = process_image_pipeline(
            session=session,
            file_path=test_img,
            source_type=IngestionSource.CLI_INPUT,
        )

        assert job.id is not None
        assert job.company_name == "Razorpay"
        assert job.role_title == "Backend Engineering Intern"
        assert job.eligibility_status == EligibilityStatus.ELIGIBLE
        assert job.raw_ingestion_id is not None
        assert job.is_official_portal is True


def test_image_pipeline_live_search_and_gap_filling(session: Session, tmp_path: Path):
    """Verify sparse screenshot without apply link triggers search grounding and fills missing gaps from live portal."""
    test_img = tmp_path / "sparse_job_screenshot.png"
    test_img.write_bytes(TINY_PNG_BYTES)

    # Image only contains minimal blurb, missing URL, skills, batch years, and deadline
    sparse_extracted = ExtractedJobData(
        company_name="Stripe",
        role_title="Software Engineering Intern",
        locations=[],
        application_deadline=None,
        application_url=None,
        allowed_grad_years=[],
        is_internship=True,
        required_skills=[],
        preferred_skills=[],
        eligibility_notes="Sparse visual snippet",
        confidence_score=0.75,
    )

    # Scraped live portal data
    live_scraped_data = ExtractedJobData(
        company_name="Stripe",
        role_title="Software Engineering Intern",
        locations=["Bangalore"],
        application_deadline="2026-12-15",
        application_url="https://stripe.com/jobs/swe-intern-2026",
        allowed_grad_years=[2028],
        is_internship=True,
        required_skills=["Python", "Go", "SQL"],
        preferred_skills=["Docker", "Distributed Systems"],
        eligibility_notes="Official portal listing for 2028 batch",
        confidence_score=0.98,
    )

    from src.services.scraper import ScrapeResult

    with patch("src.services.pipeline.extract_job_from_image", return_value=sparse_extracted), \
         patch("src.services.pipeline.search_official_career_portal", return_value="https://stripe.com/jobs/swe-intern-2026"), \
         patch("src.services.pipeline.scrape_url", return_value=ScrapeResult(url="https://stripe.com/jobs/swe-intern-2026", final_url="https://stripe.com/jobs/swe-intern-2026", success=True, text="Stripe SWE Intern 2026 detailed job description with Python, Go, and SQL requirements.")), \
         patch("src.services.pipeline.extract_job_from_text", return_value=live_scraped_data):

        job = process_image_pipeline(
            session=session,
            file_path=test_img,
            source_type=IngestionSource.TELEGRAM_IMAGE,
        )

        # 1. Verify direct application link was discovered and populated
        assert job.application_url == "https://stripe.com/jobs/swe-intern-2026"

        # 2. Verify information gaps were filled from the live portal
        assert "Python" in job.required_skills
        assert "Go" in job.required_skills
        assert "SQL" in job.required_skills
        assert "Docker" in job.preferred_skills
        assert job.allowed_grad_years == [2028]
        assert job.locations == ["Bangalore"]
        assert job.application_deadline is not None

        # 3. Verify audit trail and notes recorded gap-filling
        assert "Gaps Filled" in job.notes
        assert job.eligibility_status == EligibilityStatus.ELIGIBLE
