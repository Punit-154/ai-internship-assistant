"""Tests for Structured Extractor Service (Mocked LLM)."""

import json
from unittest.mock import MagicMock

import pytest

from src.services.extractor import (
    ExtractedJobData,
    extract_job_from_text,
    extract_jobs_list_from_text,
    extract_multiple_jobs_from_text,
    validate_gemini_api_key,
)


def test_validate_gemini_api_key_accepts_real_formats():
    """Classic AIzaSy keys and new AQ.-format keys both pass validation."""
    classic = "AIzaSy" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert len(classic) >= 40
    assert validate_gemini_api_key(classic) == classic

    new_format = "AQ." + "Ab8RN6IDXgJWx_buCzuByRh5ulQsZAFxG0Dnvv1cloY2rQb1BBA"
    assert len(new_format) >= 53
    assert validate_gemini_api_key(new_format) == new_format


def test_validate_gemini_api_key_rejects_placeholder_and_empty():
    """Placeholder and empty keys fail with a pointer to the key page."""
    with pytest.raises(ValueError, match="aistudio.google.com"):
        validate_gemini_api_key("AIzaSy_your_gemini_api_key_here")
    with pytest.raises(ValueError, match="aistudio.google.com"):
        validate_gemini_api_key(None)


def test_validate_gemini_api_key_rejects_truncated_and_garbled():
    """Truncated keys (missing 'AQ.' prefix) and short garbage must fail fast."""
    truncated = "Ab8RN6IDXgJWx_buCzuByRh5ulQsZAFxG0Dnvv1cloY2rQb1BBA"  # 50 chars, no AQ. prefix
    with pytest.raises(ValueError, match="AQ\\."):
        validate_gemini_api_key(truncated)
    with pytest.raises(ValueError, match="valid AI Studio API key"):
        validate_gemini_api_key("AQ.Ab8RN6")  # right prefix, way too short
    with pytest.raises(ValueError, match="valid AI Studio API key"):
        validate_gemini_api_key("short-key-1234567890")


def test_extracted_job_data_model():
    """Verify validation and serialization of ExtractedJobData schema."""
    data = ExtractedJobData(
        company_name="Microsoft",
        role_title="Software Engineering Intern",
        locations=["Hyderabad", "Bangalore"],
        application_deadline="2026-11-30",
        application_url="https://careers.microsoft.com/jobs/123",
        allowed_grad_years=[2027, 2028],
        required_skills=["C++", "Data Structures", "Algorithms"],
        preferred_skills=["Azure", "Distributed Systems"],
        eligibility_notes="Targeting pre-final and sophomore students",
        confidence_score=0.95,
    )
    assert data.company_name == "Microsoft"
    assert data.allowed_grad_years == [2027, 2028]
    assert data.is_internship is True


def test_extract_job_from_text_mocked():
    """Test LLM extraction function with a mocked GenAI client."""
    mock_payload = {
        "company_name": "Stripe",
        "role_title": "Backend Engineering Intern",
        "locations": ["Remote", "Bengaluru"],
        "application_deadline": "2026-10-31",
        "application_url": "https://stripe.com/jobs/123",
        "allowed_grad_years": [2028],
        "is_internship": True,
        "required_skills": ["Python", "SQL", "APIs"],
        "preferred_skills": ["Distributed Systems"],
        "eligibility_notes": "Open to 2028 graduates pursuing B.Tech CS",
        "confidence_score": 0.90,
    }

    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_payload)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    sample_text = (
        "Stripe is hiring a Backend Engineering Intern in Bengaluru or Remote. "
        "Must graduate in 2028. Required skills: Python, SQL. Deadline: Oct 31, 2026."
    )

    result = extract_job_from_text(text=sample_text, client=mock_client)

    assert result.company_name == "Stripe"
    assert result.role_title == "Backend Engineering Intern"
    assert result.allowed_grad_years == [2028]
    assert "Python" in result.required_skills
    assert result.application_deadline == "2026-10-31"


def test_extract_job_short_text_error():
    """Verify ValueError is raised when input text is too short."""
    with pytest.raises(ValueError, match="Input text is too short"):
        extract_job_from_text("Too short")


# ==============================================================================
# Batch & List Extractors (used by the discovery engine)
# ==============================================================================

def test_extract_multiple_jobs_from_text_mocked():
    """Verify blocks -> bundle alignment, with a missing slot mapped to None."""
    bundle_payload = {
        "jobs": [
            {"index": 1, "data": {
                "company_name": "Lever Co", "role_title": "Backend Intern",
                "locations": ["Remote"], "allowed_grad_years": [2028],
                "required_skills": ["Python", "SQL"], "preferred_skills": [],
                "eligibility_notes": "", "confidence_score": 0.9,
            }},
            {"index": 3, "data": {
                "company_name": "Lever Co", "role_title": "QA Intern",
                "locations": ["Boston"], "allowed_grad_years": [],
                "required_skills": ["Java"], "preferred_skills": [],
                "eligibility_notes": "", "confidence_score": 0.8,
            }},
        ]
    }
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value.text = json.dumps(bundle_payload)

    results = extract_multiple_jobs_from_text(["block one", "unparseable", "block three"], client=mock_client)

    assert results[0] is not None
    assert results[0].role_title == "Backend Intern"
    assert results[1] is None
    assert results[2] is not None
    assert results[2].role_title == "QA Intern"


def test_extract_multiple_jobs_empty_input():
    assert extract_multiple_jobs_from_text([]) == []


def test_extract_multiple_jobs_never_raises(monkeypatch):
    """LLM outage must degrade to None slots, not crash the discovery run."""
    from src.services import extractor as extractor_module

    def boom(*args, **kwargs):
        raise RuntimeError("gemini down")
    monkeypatch.setattr(extractor_module, "generate_content_with_fallback", boom)

    results = extract_multiple_jobs_from_text(["some block"], client=MagicMock())
    assert results == [None]


def test_extract_jobs_list_from_text_mocked():
    """Careers-page listing extraction returns multiple distinct postings."""
    list_payload = {
        "jobs": [
            {"company_name": "CustomCorp", "role_title": "SWE Intern",
             "locations": ["Remote"], "required_skills": ["Python"],
             "eligibility_notes": "", "confidence_score": 0.9},
            {"company_name": "CustomCorp", "role_title": "Senior Engineer",
             "locations": ["NYC"], "required_skills": ["Go"],
             "eligibility_notes": "", "confidence_score": 0.9},
        ]
    }
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value.text = json.dumps(list_payload)

    jobs = extract_jobs_list_from_text(
        "CustomCorp careers page listing multiple open engineering positions and postings across teams.",
        client=mock_client,
    )
    assert len(jobs) == 2
    assert jobs[0].role_title == "SWE Intern"


def test_extract_jobs_list_empty_or_failed():
    assert extract_jobs_list_from_text("") == []
