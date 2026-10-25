"""Tests for ATS Resume Tailoring and Pure-Python Vector PDF Generator (Milestone 6)."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.models.candidate import CandidateProfile, ProjectItem
from src.models.job import JobPosting
from src.services import resume as resume_mod
from src.services.resume import (
    ResumeTailorLLMResponse,
    TailoredProjectItem,
    compute_resume_base_name,
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
        notes="Microservices and REST APIs developed",
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


def test_compute_resume_base_name_and_filler_default():
    """Filename stem stays deterministic and is_filler defaults to False."""
    job = JobPosting(id=7, company_name="Acme Robotics", role_title="Backend Intern")
    assert compute_resume_base_name(job) == "Resume_AcmeRobotics_BackendIntern_Job7"

    item = TailoredProjectItem(title="X", tech_stack=["Python"], bullet_points=["Did y."])
    assert item.is_filler is False


def test_rebuild_tailored_resume_payload_selection(tmp_path: Path):
    """Cached payload replay honors filler subset selection at zero LLM cost."""
    profile = CandidateProfile(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
        verified_skills=["Python", "FastAPI"],
    )
    job = JobPosting(id=7, company_name="Acme", role_title="Backend Intern")

    payload = ResumeTailorLLMResponse(
        match_rationale="Backend match.",
        languages=["Python"],
        frameworks=["FastAPI"],
        tools_and_cloud=["Docker"],
        core_concepts=["REST APIs"],
        tailored_projects=[
            TailoredProjectItem(title="AI Assistant", tech_stack=["Python", "FastAPI"],
                                bullet_points=["Built pipeline."]),
            TailoredProjectItem(title="Scraper Suite", tech_stack=["Python"],
                                bullet_points=["Scraped ATS boards."]),
            TailoredProjectItem(title="Chess Engine", tech_stack=["C++"],
                                bullet_points=["Bitboard movegen."], is_filler=True),
            TailoredProjectItem(title="Task Queue", tech_stack=["Python", "Redis"],
                                bullet_points=["10k tps."], is_filler=True),
        ],
        linkedin_referral_pitch="Hello, requesting a referral.",
        cold_email_draft="Subject: Backend Intern - Punit",
    )
    payload_path = tmp_path / f"{compute_resume_base_name(job)}_payload.json"
    payload_path.write_text(payload.model_dump_json(), encoding="utf-8")

    # Keep only filler rank 2 (Task Queue)
    with patch.object(resume_mod, "get_all_stored_projects", return_value=[]):
        res = resume_mod.rebuild_tailored_resume(profile, job, payload_path, keep_fillers=[2])

    assert res.pdf_file_path.exists()
    assert res.pdf_file_path.stat().st_size > 500
    assert res.md_file_path.exists()
    md = res.markdown_content
    assert "AI Assistant" in md and "Scraper Suite" in md
    assert "Task Queue" in md
    assert "Chess Engine" not in md
    assert res.generated_filler_titles == ["Chess Engine", "Task Queue"]

    # Dropping all fillers
    with patch.object(resume_mod, "get_all_stored_projects", return_value=[]):
        res2 = resume_mod.rebuild_tailored_resume(profile, job, payload_path, keep_fillers=[])
    assert "Task Queue" not in res2.markdown_content

    # Out-of-range rank is rejected with a useful message
    with patch.object(resume_mod, "get_all_stored_projects", return_value=[]), pytest.raises(ValueError) as exc:
        resume_mod.rebuild_tailored_resume(profile, job, payload_path, keep_fillers=[9])
    assert "fillers" in str(exc.value)

    # Missing payload surfaces clear remediation
    with pytest.raises(FileNotFoundError) as exc2:
        resume_mod.rebuild_tailored_resume(profile, job, tmp_path / "missing_payload.json")
    assert "/tailor" in str(exc2.value)


def test_tailor_resume_persists_payload(tmp_path: Path):
    """Fresh tailoring writes the structured payload next to MD/PDF for later /fsel rebuilds."""
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
                "bullet_points": ["Engineered autonomous pipeline."],
            },
            {
                "title": "Task Queue",
                "tech_stack": ["Redis"],
                "bullet_points": ["10k tps Redis worker pool."],
                "is_filler": True,
            },
        ],
        "linkedin_referral_pitch": "Hi, requesting a referral for SWE Intern role.",
        "cold_email_draft": "Subject: SWE Intern - Punit",
    }
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(mock_llm_json)
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path2 = Path(tmp_dir)
        result = tailor_resume_for_job(profile, job, output_dir=tmp_path2, client=mock_client)

        assert result.payload_file_path is not None
        payload_data = json.loads(result.payload_file_path.read_text(encoding="utf-8"))
        projects = payload_data["tailored_projects"]
        assert len(projects) == 2
        assert projects[1]["is_filler"] is True
        assert result.generated_filler_titles == ["Task Queue"]


def _mocked_client_with_fillers(n_fillers: int) -> MagicMock:
    """LLM mock returning 1 primary + n_fillers filler candidates."""
    primaries = [{
        "title": "AI Internship Assistant",
        "tech_stack": ["Python", "FastAPI"],
        "bullet_points": ["Engineered autonomous pipeline."],
    }]
    fillers = [
        {
            "title": f"Filler Candidate {i}",
            "tech_stack": ["Python"],
            "bullet_points": [f"Truthful bullet {i}."],
            "is_filler": True,
        }
        for i in range(1, n_fillers + 1)
    ]
    mock_llm_json = {
        "match_rationale": "Direct match.",
        "languages": ["Python"],
        "frameworks": ["FastAPI"],
        "tools_and_cloud": ["Git"],
        "core_concepts": ["REST APIs"],
        "tailored_projects": primaries + fillers,
        "linkedin_referral_pitch": "Hi, requesting a referral.",
        "cold_email_draft": "Subject: Intern - Punit",
    }
    mock_resp = MagicMock()
    mock_resp.text = json.dumps(mock_llm_json)
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp
    return mock_client


def test_tailor_purges_fillers_over_cap_of_three(tmp_path: Path):
    """Locked cap: only ranks 1-3 of filler candidates survive into payload, MD, and result."""
    profile = CandidateProfile(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
        verified_skills=["Python", "Git"],
        verified_projects=[
            ProjectItem(title="AI Internship Assistant", tech_stack=["Python", "FastAPI"],
                        description="Autonomous ingestion pipeline"),
        ],
    )
    job = JobPosting(id=99, company_name="Figma", role_title="Software Engineering Intern")

    result = tailor_resume_for_job(
        profile, job, output_dir=tmp_path, client=_mocked_client_with_fillers(5)
    )

    assert result.generated_filler_titles == ["Filler Candidate 1", "Filler Candidate 2", "Filler Candidate 3"]

    payload_data = json.loads(result.payload_file_path.read_text(encoding="utf-8"))
    filler_entries = [p for p in payload_data["tailored_projects"] if p.get("is_filler")]
    assert [f["title"] for f in filler_entries] == result.generated_filler_titles

    md = result.md_file_path.read_text(encoding="utf-8")
    for rank in (1, 2, 3):
        assert f"Filler Candidate {rank}" in md
    assert "Filler Candidate 4" not in md
    assert "Filler Candidate 5" not in md
    assert result.pdf_file_path.exists()


def test_rebuild_purges_fillers_from_legacy_payload(tmp_path: Path):
    """Payloads written before the 3-filler lock are trimmed on load; /fsel ranks stay 1-3."""
    profile = CandidateProfile(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
        verified_skills=["Python", "FastAPI"],
    )
    job = JobPosting(id=7, company_name="Acme", role_title="Backend Intern")

    payload = ResumeTailorLLMResponse(
        match_rationale="Backend match.",
        languages=["Python"],
        frameworks=["FastAPI"],
        tools_and_cloud=["Docker"],
        core_concepts=["REST APIs"],
        tailored_projects=[
            TailoredProjectItem(title="AI Assistant", tech_stack=["Python", "FastAPI"],
                                bullet_points=["Built pipeline."]),
            TailoredProjectItem(title="Scalper", tech_stack=["Python"],
                                bullet_points=["Scraped postings."]),
        ] + [
            TailoredProjectItem(title=f"Legacy Filler {i}", tech_stack=["Python"],
                                bullet_points=[f"Bullet {i}."], is_filler=True)
            for i in range(1, 6)
        ],
        linkedin_referral_pitch="Hello, requesting a referral.",
        cold_email_draft="Subject: Backend Intern - Punit",
    )
    payload_path = tmp_path / f"{compute_resume_base_name(job)}_payload.json"
    payload_path.write_text(payload.model_dump_json(), encoding="utf-8")

    with patch.object(resume_mod, "get_all_stored_projects", return_value=[]):
        res = resume_mod.rebuild_tailored_resume(profile, job, payload_path)

    assert res.generated_filler_titles == [f"Legacy Filler {i}" for i in range(1, 4)]
    md = res.markdown_content
    assert "Legacy Filler 3" in md
    assert "Legacy Filler 4" not in md
    assert "Legacy Filler 5" not in md

    # Once trimmed, ranks beyond 3 are structurally impossible
    with patch.object(resume_mod, "get_all_stored_projects", return_value=[]), pytest.raises(ValueError) as exc:
        resume_mod.rebuild_tailored_resume(profile, job, payload_path, keep_fillers=[4])
    assert "3 fillers" in str(exc.value)