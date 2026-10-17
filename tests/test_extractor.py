"""Tests for Structured Extractor Service (Mocked LLM)."""

import json
from unittest.mock import MagicMock
import pytest
from src.services.extractor import ExtractedJobData, extract_job_from_text


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
