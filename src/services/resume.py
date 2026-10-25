"""Truthful ATS Resume Tailoring, Keyword Extraction, and PDF Generation Service.

Implements zero-fabrication, ATS-optimized resume tailoring:
- Extracts target ATS keywords and skills from the job posting.
- Ingests deep project context and READMEs from the dedicated knowledge base.
- Infuses verified matched keywords naturally into technical skills and STAR project bullets.
- Generates a full ATS Keyword Match Audit (Match Score %, Matched Keywords, Missing Gaps).
- Produces a single-page vector ATS PDF resume (pure-Python, zero native dependencies).
- Produces a companion Markdown (.md) resume copy.
- Generates high-conversion LinkedIn referral pitches and cold email drafts.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from src.models.candidate import CandidateProfile
from src.models.job import JobPosting
from src.services.extractor import generate_content_with_fallback, get_genai_client
from src.services.github_service import get_all_stored_projects
from src.services.pdf_generator import generate_resume_pdf
from src.services.skills import find_matching_skill

logger = logging.getLogger(__name__)


@dataclass
class ATSAuditReport:
    """Detailed ATS keyword alignment audit."""
    target_keywords: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    missing_keywords: List[str] = field(default_factory=list)
    match_percentage: float = 0.0


class TailoredProjectItem(BaseModel):
    """Structured tailored project entry."""
    title: str = Field(description="Title of project matching candidate verified projects")
    tech_stack: List[str] = Field(description="Verified technologies used in this project")
    bullet_points: List[str] = Field(
        description="2-3 quantified, action-oriented bullet points using Google X-Y-Z formula naturally incorporating verified ATS keywords"
    )
    github_url: Optional[str] = Field(default=None, description="GitHub URL if available")


class ResumeTailorLLMResponse(BaseModel):
    """Structured LLM output for ATS optimization."""
    match_rationale: str = Field(description="Brief explanation of how candidate profile aligns with the target role")
    languages: List[str] = Field(description="Programming languages from candidate verified skills, prioritized for this job")
    frameworks: List[str] = Field(description="Frameworks and libraries from candidate verified skills, prioritized for this job")
    tools_and_cloud: List[str] = Field(description="Developer tools, databases, and cloud from candidate verified skills")
    core_concepts: List[str] = Field(description="Computer science concepts and methodologies from candidate verified skills")
    tailored_projects: List[TailoredProjectItem] = Field(description="Up to 3 top matching projects selected strictly from candidate verified projects")
    linkedin_referral_pitch: str = Field(
        description="A polite, crisp (<100 words) LinkedIn outreach/referral request for alumni or recruiters at the company"
    )
    cold_email_draft: str = Field(
        description="A concise, professional cold email draft (<120 words) with subject line"
    )


@dataclass
class TailoredResumeResult:
    """Complete result of the resume tailoring process."""
    job_id: int
    company_name: str
    role_title: str
    pdf_file_path: Path
    md_file_path: Path
    markdown_content: str
    linkedin_referral_pitch: str
    cold_email_draft: str
    match_rationale: str
    matching_skills: List[str]
    ats_audit: ATSAuditReport


def extract_ats_keywords_from_job(job: JobPosting) -> List[str]:
    """Extract discrete technical keywords and competencies from the job description.

    Alias-equivalent keywords are deduplicated (e.g., "DRF" folded into
    "Django REST Framework" keeps only whichever appears first).
    """
    keywords: List[str] = []
    for s in (job.required_skills + job.preferred_skills):
        clean = s.strip()
        if not clean or len(clean) < 2:
            continue
        if find_matching_skill(clean, keywords) is None:
            keywords.append(clean)

    text_corpus = f"{job.role_title} {job.notes or ''}".lower()
    common_terms = [
        "python", "java", "c++", "go", "golang", "typescript", "javascript",
        "fastapi", "django", "flask", "spring boot", "react", "node.js", "next.js",
        "sql", "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
        "docker", "kubernetes", "aws", "gcp", "azure", "git", "ci/cd",
        "rest apis", "restful", "microservices", "distributed systems", "data structures",
        "algorithms", "object-oriented programming", "system design", "linux"
    ]
    for term in common_terms:
        if term not in text_corpus:
            continue
        display = term.title() if term not in ("sql", "aws", "gcp", "ci/cd") else term.upper()
        if find_matching_skill(display, keywords) is None:
            keywords.append(display)

    return keywords


def audit_ats_keywords(
    target_keywords: List[str],
    profile: CandidateProfile,
    stored_projects: Optional[List[Dict]] = None,
) -> ATSAuditReport:
    """Cross-reference job ATS keywords against candidate verified skills and project READMEs.

    Uses alias-aware matching (src/services/skills.py) so acronym forms
    such as "DRF" resolve against "Django REST Framework".
    """
    verified_pool: List[str] = list(profile.verified_skills)
    verified_pool.extend(
        t for proj in profile.verified_projects for t in proj.tech_stack
    )
    if stored_projects:
        verified_pool.extend(
            t
            for p in stored_projects
            for t in p.get("meta", {}).get("tech_stack", [])
        )
    verified_pool = list(dict.fromkeys(v.strip() for v in verified_pool if v.strip()))

    matched = []
    missing = []

    for kw in target_keywords:
        if find_matching_skill(kw, verified_pool):
            matched.append(kw)
        else:
            missing.append(kw)

    total = len(target_keywords)
    # Zero extracted keywords means nothing was demonstrated: report 0% rather
    # than a misleading 100% match (e.g. when an LLM extraction came back empty).
    pct = (len(matched) / total * 100.0) if total > 0 else 0.0

    return ATSAuditReport(
        target_keywords=target_keywords,
        matched_keywords=matched,
        missing_keywords=missing,
        match_percentage=round(pct, 1),
    )


def render_resume_markdown(
    profile: CandidateProfile,
    job: JobPosting,
    tailored: ResumeTailorLLMResponse,
    ats_audit: Optional[ATSAuditReport] = None,
) -> str:
    """Render a clean, human-readable Markdown companion resume."""
    md_lines = [
        f"# {profile.full_name} - ATS Tailored Resume",
        f"**Target Role:** {job.role_title} at {job.company_name} (Job #{job.id})",
        "",
    ]

    if ats_audit:
        matched_str = ", ".join(ats_audit.matched_keywords) or "None"
        missing_str = ", ".join(ats_audit.missing_keywords) or "None"
        md_lines.extend([
            f"> **ATS Keyword Match Score:** {ats_audit.match_percentage}%",
            f"> **Verified Matched Keywords:** {matched_str}",
            f"> **Missing Skill Gaps (Omitted):** {missing_str}",
            "",
        ])

    md_lines.extend([
        "## Education",
        f"- **{profile.college}** | {profile.degree} (Expected Graduation: {profile.graduation_year})",
        "",
        "## Technical Skills",
        f"- **Languages:** {', '.join(tailored.languages)}",
        f"- **Frameworks & Libraries:** {', '.join(tailored.frameworks)}",
        f"- **Developer Tools & Cloud:** {', '.join(tailored.tools_and_cloud)}",
        f"- **Core Concepts:** {', '.join(tailored.core_concepts)}",
        "",
        "## Selected Projects",
    ])

    for proj in tailored.tailored_projects:
        md_lines.append(f"### {proj.title} ({', '.join(proj.tech_stack)})")
        if proj.github_url:
            md_lines.append(f"GitHub: [{proj.github_url}]({proj.github_url})")
        for b in proj.bullet_points:
            md_lines.append(f"- {b}")
        md_lines.append("")

    md_lines.extend([
        "---",
        "## LinkedIn Referral Request Pitch",
        f"> {tailored.linkedin_referral_pitch}",
        "",
        "## Cold Outreach Email Draft",
        "```",
        tailored.cold_email_draft,
        "```",
    ])
    return "\n".join(md_lines)


def tailor_resume_for_job(
    profile: CandidateProfile,
    job: JobPosting,
    output_dir: Optional[Path] = None,
    client: Optional[genai.Client] = None,
) -> TailoredResumeResult:
    """Generate a truthful, ATS-optimized PDF resume and keyword audit."""
    if not output_dir:
        output_dir = Path("resumes")
    output_dir.mkdir(parents=True, exist_ok=True)

    ai_client = client or get_genai_client()

    # 1. Gather stored GitHub project knowledge base
    stored_projects = get_all_stored_projects()
    project_context_blocks = []
    for sp in stored_projects:
        meta = sp.get("meta", {})
        readme_snippet = sp.get("readme", "")[:1200]
        project_context_blocks.append(
            f"PROJECT: {meta.get('title')}\nTech Stack: {meta.get('tech_stack')}\nSummary: {meta.get('description')}\nREADME Highlights:\n{readme_snippet}\n"
        )
    projects_deep_text = "\n---\n".join(project_context_blocks)

    # 2. Extract ATS Keywords and perform audit
    target_keywords = extract_ats_keywords_from_job(job)
    ats_audit = audit_ats_keywords(target_keywords, profile, stored_projects)

    prompt = f"""
You are an expert technical resume coach and ATS optimization specialist.
You must tailor an ATS-optimized, truthful resume for a candidate targeting an internship opportunity.

CANDIDATE PROFILE (STRICT ZERO-FABRICATION CONSTRAINT):
Name: {profile.full_name}
College: {profile.college}
Degree: {profile.degree}
Graduation Year: {profile.graduation_year} (Current Year {profile.current_college_year})
Verified Skills: {json.dumps(profile.verified_skills)}
Verified Projects: {json.dumps([p.model_dump() for p in profile.verified_projects])}

DEEP GITHUB PROJECT KNOWLEDGE BASE:
{projects_deep_text}

TARGET JOB POSTING:
Company: {job.company_name}
Role Title: {job.role_title}
Locations: {json.dumps(job.locations)}
Required Skills: {json.dumps(job.required_skills)}
Preferred Skills: {json.dumps(job.preferred_skills)}
Job ATS Keywords to naturally incorporate: {json.dumps(ats_audit.matched_keywords)}
Missing Skills (DO NOT INVENT): {json.dumps(ats_audit.missing_keywords)}

RULES:
1. STRICT ZERO-FABRICATION: You may ONLY use skills and projects present in the candidate's verified profile or GitHub knowledge base. NEVER invent companies, degrees, dates, tools, or technologies.
2. ATS TAILORING & KEYWORD ALIGNMENT: Prioritize matched ATS keywords in technical skills. Frame project bullet points using Google X-Y-Z formula ('Accomplished [X] measured by [Y] by doing [Z]') emphasizing verified matched keywords.
3. REFERRAL PITCH: Draft a crisp (<100 words), polite LinkedIn message to VIT alumni or recruiters requesting a referral.
4. COLD EMAIL: Draft a concise (<120 words) email with subject line to the hiring team.
"""

    response = generate_content_with_fallback(
        client=ai_client,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ResumeTailorLLMResponse,
            temperature=0.2,
        ),
    )

    tailored_llm = ResumeTailorLLMResponse.model_validate_json(response.text)

    # Attach github URLs from verified profile if available
    project_github_map = {
        p.title.lower(): p.github_url for p in profile.verified_projects if p.github_url
    }
    for sp in stored_projects:
        meta = sp.get("meta", {})
        if meta.get("github_url"):
            project_github_map[meta.get("title", "").lower()] = meta["github_url"]

    for proj in tailored_llm.tailored_projects:
        for orig_title, url in project_github_map.items():
            if orig_title in proj.title.lower() or proj.title.lower() in orig_title:
                proj.github_url = url
                break

    markdown_content = render_resume_markdown(profile, job, tailored_llm, ats_audit)

    # Clean filenames
    safe_company = "".join(c for c in job.company_name if c.isalnum() or c in ("-", "_")).strip() or "Company"
    safe_role = "".join(c for c in job.role_title if c.isalnum() or c in ("-", "_")).strip() or "Role"
    base_filename = f"Resume_{safe_company}_{safe_role}_Job{job.id}"

    md_path = output_dir / f"{base_filename}.md"
    pdf_path = output_dir / f"{base_filename}.pdf"

    md_path.write_text(markdown_content, encoding="utf-8")

    # Guaranteed Pure-Python ATS PDF Generation
    generate_resume_pdf(profile, tailored_llm, pdf_path)
    logger.info(f"Generated clean ATS PDF resume at: {pdf_path}")

    return TailoredResumeResult(
        job_id=job.id or 0,
        company_name=job.company_name,
        role_title=job.role_title,
        pdf_file_path=pdf_path,
        md_file_path=md_path,
        markdown_content=markdown_content,
        linkedin_referral_pitch=tailored_llm.linkedin_referral_pitch,
        cold_email_draft=tailored_llm.cold_email_draft,
        match_rationale=tailored_llm.match_rationale,
        matching_skills=ats_audit.matched_keywords,
        ats_audit=ats_audit,
    )
