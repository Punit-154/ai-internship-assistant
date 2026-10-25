"""Tests for Pure-Python ATS PDF Resume Generator."""

from pathlib import Path

from src.models.candidate import CandidateProfile
from src.services.pdf_generator import (
    _LAST_FIT_STATS,
    PDFResumeBuilder,
    generate_resume_pdf,
    sanitize_text,
)
from src.services.resume import ResumeTailorLLMResponse, TailoredProjectItem

LONG_BULLET = (
    "Engineered a resilient distributed ingestion pipeline processing 10,000 postings per minute "
    "across 3 ATS vendors with exponential backoff retries and sub-second p99 latency measured by "
    "Grafana dashboards by rewriting the scheduler to use lock-free rings."
)


def _profile(**overrides) -> CandidateProfile:
    base = dict(
        full_name="Punit",
        college="VIT Chennai",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
        verified_skills=["Python", "FastAPI", "Redis", "Docker", "AWS"],
    )
    base.update(overrides)
    return CandidateProfile(**base)


def _tailored(primary_bullets: int, n_fillers: int, bullet_text: str) -> ResumeTailorLLMResponse:
    projects = [
        TailoredProjectItem(
            title=f"Primary {i}",
            tech_stack=["Python", "FastAPI"],
            bullet_points=[bullet_text] * primary_bullets,
        )
        for i in range(3)
    ] + [
        TailoredProjectItem(
            title=f"Filler {i}",
            tech_stack=["Python", "Redis"],
            bullet_points=[bullet_text],
            is_filler=True,
        )
        for i in range(n_fillers)
    ]
    return ResumeTailorLLMResponse(
        match_rationale="Solid backend alignment",
        languages=["Python"],
        frameworks=["FastAPI"],
        tools_and_cloud=["Docker"],
        core_concepts=["REST APIs"],
        tailored_projects=projects,
        linkedin_referral_pitch="Hi! Would love a referral.",
        cold_email_draft="Subject: Application\nHello team...",
    )


def _pdf_page_count(pdf_bytes: bytes) -> int:
    """Naive uncompressed-ReportLab object count; works for our single-template output."""
    return pdf_bytes.count(b"/Type /Page") - pdf_bytes.count(b"/Type /Pages")


def test_sanitize_text():
    """Verify smart quotes, dashes, and bullet symbols are properly normalized."""
    text = "Engineered \u201cReal-Time\u201d logger \u2013 99.9% uptime \u2022 Low latency"
    sanitized = sanitize_text(text)
    assert '"Real-Time"' in sanitized
    assert "-" in sanitized
    assert "*" in sanitized


def test_pdf_builder_creates_valid_pdf_bytes():
    """Verify builder produces standard PDF 1.4 binary structure."""
    builder = PDFResumeBuilder()
    builder.render_header("Punit", "+91 9876543210 | punit@vit.ac.in")
    builder.render_section_header("Education")
    builder.render_education("VIT Chennai", "Chennai, India", "B.Tech CSE", "2028")
    builder.render_section_header("Technical Skills")
    builder.render_technical_skills(
        languages=["Python", "Go"],
        frameworks=["FastAPI"],
        tools=["Docker", "Redis"],
        concepts=["Distributed Systems"],
    )
    builder.render_section_header("Projects")
    builder.render_project(
        title="Distributed Task Queue",
        tech_stack=["Python", "Redis", "Docker"],
        bullets=["Architected worker pool processing 10k tasks/sec using Redis queues."],
        date_str="2026",
    )

    pdf_bytes = builder.compile_pdf_bytes()
    assert pdf_bytes.startswith(b"%PDF-1.4")
    assert b"%%EOF" in pdf_bytes
    assert b"/Helvetica-Bold" in pdf_bytes
    assert b"/Helvetica" in pdf_bytes
    assert b"xref" in pdf_bytes


def test_generate_resume_pdf_file_creation(tmp_path: Path):
    """Verify generate_resume_pdf writes a physical PDF file with non-zero size."""
    profile = CandidateProfile(
        full_name="Punit",
        college="Vellore Institute of Technology",
        degree="B.Tech Computer Science and Engineering",
        graduation_year=2028,
    )
    tailored = ResumeTailorLLMResponse(
        match_rationale="Solid backend alignment",
        languages=["Python", "C++"],
        frameworks=["FastAPI", "Flask"],
        tools_and_cloud=["Docker", "AWS"],
        core_concepts=["REST APIs", "Microservices"],
        tailored_projects=[
            TailoredProjectItem(
                title="Distributed Task Queue",
                tech_stack=["Python", "FastAPI", "Redis"],
                bullet_points=[
                    "Engineered worker pool with Redis queues achieving 99.9% delivery reliability."
                ],
                github_url="https://github.com/punit/task-queue",
            )
        ],
        linkedin_referral_pitch="Hi! Would love a referral.",
        cold_email_draft="Subject: Application\nHello team...",
    )

    out_file = tmp_path / "test_resume.pdf"
    result_path = generate_resume_pdf(profile, tailored, out_file)

    assert result_path.exists()
    assert result_path.stat().st_size > 500
    content = result_path.read_bytes()
    assert content.startswith(b"%PDF-1.4")
    assert b"%%EOF" in content


def test_fillers_stack_when_page_has_room(tmp_path: Path):
    """Sparse resumes fill leftover space with ranked filler projects (one page, floor kept)."""
    profile = _profile()
    tailored = _tailored(primary_bullets=2, n_fillers=3,
                         bullet_text="Built worker pool achieving 10x throughput on Redis queues.")

    out_file = tmp_path / "sparse.pdf"
    generate_resume_pdf(profile, tailored, out_file)
    content = out_file.read_bytes()

    stats = dict(_LAST_FIT_STATS)
    assert _pdf_page_count(content) == 1
    assert stats["fillers_included"] == 3
    assert stats["fill_left"] >= 0
    assert stats["estimated_total"] <= stats["page_budget"]


def test_fillers_dropped_when_page_is_dense(tmp_path: Path):
    """Dense resumes trim fillers/depth instead of spilling to page 2."""
    profile = _profile(
        summary=LONG_BULLET,
        research_experience=[
            {"title": "Samsung PRISM", "organization": "VIT", "date_range": "2025 - Present",
             "bullet_points": [LONG_BULLET, LONG_BULLET, LONG_BULLET]},
        ],
        achievements=["Winner Hackathon", "Olympiad Gold", "Dean's List"],
    )
    tailored = _tailored(primary_bullets=3, n_fillers=3, bullet_text=LONG_BULLET)

    out_file = tmp_path / "dense.pdf"
    generate_resume_pdf(profile, tailored, out_file)
    content = out_file.read_bytes()

    stats = dict(_LAST_FIT_STATS)
    assert _pdf_page_count(content) == 1
    assert stats["fillers_included"] < 3
    assert stats["estimated_total"] <= stats["page_budget"]
    assert stats["fill_left"] >= 0


def test_filler_cap_hard_limit_of_three(tmp_path: Path):
    """Locked cap: a renderer never stacks more than 3 add-on projects even with surplus room."""
    profile = _profile()
    tailored = _tailored(primary_bullets=2, n_fillers=5,
                         bullet_text="Built worker pool achieving 10x throughput on Redis queues.")

    out_file = tmp_path / "capped.pdf"
    generate_resume_pdf(profile, tailored, out_file)
    content = out_file.read_bytes()

    stats = dict(_LAST_FIT_STATS)
    assert _pdf_page_count(content) == 1
    assert stats["fillers_included"] == 3
    assert stats["fill_left"] >= 0
    assert stats["estimated_total"] <= stats["page_budget"]


def test_no_fillers_resume_fits_one_page(tmp_path: Path):
    """Legacy behavior: no filler candidates still renders exactly one page."""
    profile = _profile()
    tailored = _tailored(primary_bullets=2, n_fillers=0,
                         bullet_text="Built worker pool achieving 10x throughput on Redis queues.")

    out_file = tmp_path / "lean.pdf"
    generate_resume_pdf(profile, tailored, out_file)
    stats = dict(_LAST_FIT_STATS)

    assert _pdf_page_count(out_file.read_bytes()) == 1
    assert stats["fillers_included"] == 0
    assert stats["estimated_total"] <= stats["page_budget"]
