"""Tests for Pure-Python ATS PDF Resume Generator."""

from pathlib import Path

from src.models.candidate import CandidateProfile
from src.services.pdf_generator import (
    PDFResumeBuilder,
    generate_resume_pdf,
    sanitize_text,
)
from src.services.resume import ResumeTailorLLMResponse, TailoredProjectItem


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
