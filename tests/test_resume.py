"""Tests for ATS Resume Tailoring and Pure-Python Vector PDF Generator (Milestone 6)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.models.candidate import CandidateProfile, ProjectItem
from src.models.job import JobPosting
from src.services.resume import (
    ResumeTailorLLMResponse,
    TailoredProjectItem,
    render_resume_markdown,
    tailor_resume_for_job,
)


def test_render_resume_markdown():
    """Verify companion Markdown output contains sections, projects, and referral drafts."""
    profile = CandidateProfile(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
    )
    job = JobPosting(
        id=8,
        company_name="Interview Kickstart",
        role_title="Backend Intern",
    )
    tailored_llm = ResumeTailorLLMResponse(
        match_rationale="Matches Python & API requirements.",
        languages=["Python", "SQL"],
        frameworks=["FastAPI"],
        tools_and_cloud=["Git", "Docker"],
        core_concepts=["RESTful APIs"],
        tailored_projects=[
            TailoredProjectItem(
                title="AI Internship Assistant",
                tech_stack=["Python", "FastAPI", "SQLite"],
                bullet_points=["Built autonomous pipeline ingesting job postings."],
            )
        ],
        linkedin_referral_pitch="Hello, I would appreciate a referral for the Backend Intern role.",
        cold_email_draft="Subject: Backend Intern Role - Punit",
    )

    md = render_resume_markdown(profile, job, tailored_llm)
    assert "# Punit - ATS Tailored Resume" in md
    assert "Backend Intern at Interview Kickstart" in md
    assert "## Technical Skills" in md
    assert "Python, SQL" in md
    assert "## LinkedIn Referral Request Pitch" in md
    assert "Hello, I would appreciate a referral" in md


def test_tailor_resume_for_job_mocked():
    """Verify end-to-end resume tailoring with mocked Gemini response saves pure vector .pdf and companion .md."""
    profile = CandidateProfile(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
        verified_skills=["Python", "SQL", "Git", "FastAPI"],
        verified_projects=[
            ProjectItem(
                title="AI Internship Assistant",
                tech_stack=["Python", "FastAPI"],
                description="Autonomous ingestion pipeline",
            )
        ],
    )
    job = JobPosting(
        id=99,
        company_name="Figma",
        role_title="Software Engineering Intern",
        required_skills=["Python", "Git"],
        preferred_skills=["SQL"],
    )

    mock_llm_json = {
        "match_rationale": "Direct match for Python backend requirements.",
        "languages": ["Python", "SQL"],
        "frameworks": ["FastAPI"],
        "tools_and_cloud": ["Git"],
        "core_concepts": ["REST APIs"],
        "tailored_projects": [
            {
                "title": "AI Internship Assistant",
                "tech_stack": ["Python", "FastAPI"],
                "bullet_points": [
                    "Engineered autonomous pipeline processing job postings in sub-seconds.",
                ],
            }
        ],
        "linkedin_referral_pitch": "Hi [Name], reaching out regarding the SWE Intern role at Figma.",
        "cold_email_draft": "Subject: SWE Intern Application - Punit",
    }

    mock_resp = MagicMock()
    mock_resp.text = json.dumps(mock_llm_json)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        result = tailor_resume_for_job(profile, job, output_dir=tmp_path, client=mock_client)

        assert result.job_id == 99
        assert result.company_name == "Figma"
        assert result.pdf_file_path.exists()
        assert result.pdf_file_path.suffix == ".pdf"
        assert result.pdf_file_path.stat().st_size > 500
        assert "Figma" in result.pdf_file_path.name
        assert result.md_file_path.exists()
        assert result.md_file_path.suffix == ".md"
        assert result.ats_audit.match_percentage > 50.0
        assert len(result.matching_skills) >= 2


def test_ats_keyword_extraction_and_audit():
    """Verify ATS keywords are extracted from job and audited against profile."""
    from src.services.resume import audit_ats_keywords, extract_ats_keywords_from_job
    job = JobPosting(
        role_title="Backend Software Engineering Intern",
        required_skills=["Python", "FastAPI", "PostgreSQL"],
        preferred_skills=["Docker", "Kafka"],
        notes="Microservices and REST APIs REDACTED",
    )
    kws = extract_ats_keywords_from_job(job)
    assert "Python" in kws
    assert "FastAPI" in kws
    assert "PostgreSQL" in kws
    assert "Docker" in kws

    profile = CandidateProfile(
        verified_skills=["Python", "FastAPI", "Docker", "Git"],
    )
    audit = audit_ats_keywords(kws, profile)
    assert "Python" in audit.matched_keywords
    assert "FastAPI" in audit.matched_keywords
    assert "Docker" in audit.matched_keywords
    assert "Kafka" in audit.missing_keywords
    assert audit.match_percentage > 40.0