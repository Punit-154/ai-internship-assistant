"""Candidate Profile Management and Intelligent Opportunity Matching Service.

Maintains candidate academic state, career goals, and evaluates incoming
job postings against personalized graduation, location, and skill criteria.
Also provides intelligent parsing for adding new projects into the portfolio.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from google import genai
from google.genai import types
from src.config import settings
from src.models.candidate import CandidateProfile, ProjectItem
from src.models.job import EligibilityStatus

logger = logging.getLogger(__name__)

DEFAULT_PROFILE_PATH = settings.BASE_DIR / "data" / "candidate_profile.json"


@dataclass
class MatchEvaluation:
    """Detailed multidimensional match assessment for a job posting."""
    batch_status: EligibilityStatus
    batch_reason: str
    role_match: bool
    role_notes: str
    location_fit: str
    location_is_viable: bool
    matching_skills: List[str]
    missing_skills: List[str]
    overall_match_score: float  # 0.0 to 1.0


def get_candidate_profile(file_path: Optional[Path] = None) -> CandidateProfile:
    """Load the candidate profile from disk, creating default if missing."""
    target_path = file_path or DEFAULT_PROFILE_PATH
    if not target_path.exists():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        default_profile = CandidateProfile()
        save_candidate_profile(default_profile, target_path)
        return default_profile

    try:
        with open(target_path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return CandidateProfile.model_validate(data)
    except Exception as exc:
        logger.error(f"Error loading candidate profile from {target_path}: {exc}")
        return CandidateProfile()


def save_candidate_profile(profile: CandidateProfile, file_path: Optional[Path] = None) -> None:
    """Save candidate profile to disk."""
    target_path = file_path or DEFAULT_PROFILE_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(profile.model_dump_json(indent=2))


def update_candidate_profile(updates: Dict[str, Any], file_path: Optional[Path] = None) -> CandidateProfile:
    """Update specific fields in candidate profile and persist changes."""
    profile = get_candidate_profile(file_path)
    profile_data = profile.model_dump()

    for key, value in updates.items():
        if key in profile_data:
            profile_data[key] = value
        else:
            raise KeyError(f"Unknown profile field: '{key}'. Valid fields: {list(profile_data.keys())}")

    updated = CandidateProfile.model_validate(profile_data)
    save_candidate_profile(updated, file_path)
    return updated


# ==============================================================================
# Intelligent Project Parsing & Ingestion
# ==============================================================================

PROJECT_PARSER_SYSTEM_INSTRUCTION = """
You are a senior technical resume architect and project portfolio analyzer.
Your task is to parse raw, informal descriptions of a student's personal or academic project
into a high-impact, professional ProjectItem schema.

RULES:
1. TITLE: Extract a concise, professional title (e.g., 'Distributed Log Aggregator' instead of 'my cool logger').
2. TECH STACK: Identify discrete technologies, libraries, frameworks, and databases (e.g. ['Python', 'FastAPI', 'Redis', 'Docker']).
3. DESCRIPTION: Write a concise, impact-oriented summary highlighting architecture, key features, and engineering trade-offs.
4. LINKS: Extract github_url or live_demo_url if mentioned; otherwise null.
"""


def parse_project_from_text(
    text: str,
    client: Optional[genai.Client] = None,
    model_name: Optional[str] = None,
) -> ProjectItem:
    """Use Gemini Flash with structured schema to parse informal project notes into a ProjectItem."""
    if not text or len(text.strip()) < 10:
        raise ValueError("Project description is too short.")

    if not client:
        api_key = settings.GEMINI_API_KEY
        if not api_key or api_key.startswith("AIzaSy_your_gemini"):
            raise ValueError("GEMINI_API_KEY is missing or invalid in your .env file.")
        client = genai.Client(api_key=api_key)

    target_model = model_name or settings.GEMINI_MODEL

    config = types.GenerateContentConfig(
        system_instruction=PROJECT_PARSER_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=ProjectItem,
        temperature=0.1,
    )

    response = client.models.generate_content(
        model=target_model,
        contents=f"Parse the following project details:\n\n{text}",
        config=config,
    )

    data_dict = json.loads(response.text)
    return ProjectItem.model_validate(data_dict)


def add_project_to_profile(
    project: ProjectItem,
    file_path: Optional[Path] = None,
) -> Tuple[CandidateProfile, List[str]]:
    """Append a project to the profile and automatically sync any new skills discovered."""
    profile = get_candidate_profile(file_path)

    # Check if project already exists (match by github_url or title)
    existing_idx = None
    for idx, p in enumerate(profile.verified_projects):
        if project.github_url and p.github_url and project.github_url.rstrip("/").lower() == p.github_url.rstrip("/").lower():
            existing_idx = idx
            break
        if project.title.strip().lower() == p.title.strip().lower():
            existing_idx = idx
            break

    if existing_idx is not None:
        profile.verified_projects[existing_idx] = project
    else:
        profile.verified_projects.append(project)

    existing_skills_lower = {s.lower() for s in profile.verified_skills}
    newly_added_skills = []

    for tech in project.tech_stack:
        if tech.lower() not in existing_skills_lower:
            profile.verified_skills.append(tech)
            existing_skills_lower.add(tech.lower())
            newly_added_skills.append(tech)

    save_candidate_profile(profile, file_path)
    return profile, newly_added_skills


# ==============================================================================
# Job Matching & Opportunity Scoring
# ==============================================================================

def evaluate_job_match(
    role_title: str,
    locations: List[str],
    allowed_grad_years: List[int],
    required_skills: List[str],
    profile: Optional[CandidateProfile] = None,
) -> MatchEvaluation:
    """Evaluate an opportunity across batch eligibility, role relevance, location, and skills."""
    p = profile or get_candidate_profile()

    # 1. Batch / Graduation Year Check
    if not allowed_grad_years:
        batch_status = EligibilityStatus.UNCERTAIN
        batch_reason = "No graduation batch specified; open or unstated."
    elif p.graduation_year in allowed_grad_years:
        batch_status = EligibilityStatus.ELIGIBLE
        batch_reason = f"Explicitly accepts your {p.graduation_year} graduation batch."
    else:
        batch_status = EligibilityStatus.INELIGIBLE
        batch_reason = f"Restricted to batches {allowed_grad_years}. Excludes {p.graduation_year} batch."

    # 2. Role Title Relevance Match
    role_lower = role_title.lower()
    role_match = False
    role_notes = "Not aligned with primary tracks"

    relevant_keywords = [
        "software", "backend", "full stack", "fullstack", "cloud",
        "ai", "ml", "machine learning", "data", "developer", "engineer", "intern", "trainee", "consultant"
    ]
    if any(k in role_lower for k in relevant_keywords):
        role_match = True
        role_notes = "Direct match with target technical track"

    # 3. Location and Work Mode Assessment
    location_fit = "Unspecified location"
    location_is_viable = True
    locations_lower = [loc.lower() for loc in locations]

    is_remote = any("remote" in loc or "virtual" in loc or "work from home" in loc for loc in locations_lower)
    is_summer_hub = any(
        any(hub.lower() in loc for hub in p.summer_locations)
        for loc in locations_lower
    )

    if is_remote:
        location_fit = "Remote / Virtual - Perfect for immediate availability during college semester"
        location_is_viable = True
    elif is_summer_hub:
        matched_hubs = [hub for hub in p.summer_locations if any(hub.lower() in loc for loc in locations_lower)]
        location_fit = f"Nearby Summer Hub: {', '.join(matched_hubs)} - Ideal for summer break"
        location_is_viable = True
    elif locations:
        location_fit = f"Outside primary hubs ({', '.join(locations)}). {p.relocation_preference}"
        location_is_viable = True
    else:
        location_fit = "Location not listed"

    # 4. Skill Overlap Analysis
    verified_skills_lower = {s.lower() for s in p.verified_skills}
    matching_skills = []
    missing_skills = []

    for req in required_skills:
        req_clean = req.strip()
        if any(req_clean.lower() in s or s in req_clean.lower() for s in verified_skills_lower):
            matching_skills.append(req_clean)
        else:
            missing_skills.append(req_clean)

    # 5. Composite Score Calculation
    score = 0.0
    if batch_status == EligibilityStatus.ELIGIBLE:
        score += 0.40
    elif batch_status == EligibilityStatus.UNCERTAIN:
        score += 0.25

    if role_match:
        score += 0.30

    if location_is_viable:
        score += 0.15

    if required_skills:
        skill_ratio = len(matching_skills) / len(required_skills)
        score += 0.15 * skill_ratio
    else:
        score += 0.15

    return MatchEvaluation(
        batch_status=batch_status,
        batch_reason=batch_reason,
        role_match=role_match,
        role_notes=role_notes,
        location_fit=location_fit,
        location_is_viable=location_is_viable,
        matching_skills=matching_skills,
        missing_skills=missing_skills,
        overall_match_score=round(score, 2),
    )
