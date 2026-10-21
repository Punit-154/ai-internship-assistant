"""GitHub Project Ingestion and Dedicated Knowledge Base Service.

Manages persistent project repositories in `data/projects/<slug>/`:
- Fetches full raw README.md directly from GitHub repositories
- Uses Gemini Flash to perform deep extraction of architectures, tech stacks, and quantifiable metrics
- Automatically saves README.md and metadata into the dedicated projects knowledge base
- Synchronizes newly discovered technologies into the candidate profile verified skills
- Provides full project context to the ATS resume tailoring engine
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import httpx
from google import genai
from google.genai import types

from src.config import settings
from src.models.candidate import CandidateProfile, ProjectItem
from src.services.extractor import generate_content_with_fallback, get_genai_client
from src.services.profile import add_project_to_profile, get_candidate_profile

logger = logging.getLogger(__name__)

PROJECTS_DIR = settings.BASE_DIR / "data" / "projects"

GITHUB_URL_REGEX = re.compile(
    r"https?://(?:www\.)?github\.com/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_.-]+)"
)

README_ANALYZER_SYSTEM_INSTRUCTION = """
You are a Principal Software Engineering Recruiter and Portfolio Architect.
Analyze the provided GitHub repository README markdown to extract deep technical project specifications.

RULES:
1. TITLE: Extract a concise, professional title for the project.
2. TECH STACK: List ALL discrete programming languages, frameworks, libraries, databases, message queues, cloud services, and tools mentioned or used.
3. DESCRIPTION: Write a dense 2-3 sentence technical overview highlighting architecture, performance characteristics, and key engineering features.
4. GITHUB URL: Use the provided repository URL.
"""


def extract_github_repo_coords(url_or_text: str) -> Optional[Tuple[str, str, str]]:
    """Extract (owner, repo, clean_url) from a GitHub URL or text snippet."""
    match = GITHUB_URL_REGEX.search(url_or_text)
    if not match:
        return None
    owner = match.group(1)
    repo = match.group(2).rstrip("/").removesuffix(".git")
    clean_url = f"https://github.com/{owner}/{repo}"
    return owner, repo, clean_url


def fetch_github_readme(owner: str, repo: str) -> Optional[str]:
    """Fetch README.md from raw.githubusercontent.com trying main and master branches."""
    branches = ["main", "master"]
    headers = {"User-Agent": "AI-Internship-Assistant/1.0"}

    for branch in branches:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200 and len(resp.text.strip()) > 20:
                    return resp.text
        except Exception as exc:
            logger.warning(f"Failed to fetch README from {url}: {exc}")
            continue
    return None


def slugify(text: str) -> str:
    """Create a clean directory slug."""
    slug = re.sub(r"[^a-zA-Z0-9_-]", "-", text.lower()).strip("-")
    return re.sub(r"-+", "-", slug) or "project"


def save_project_to_knowledge_base(
    project: ProjectItem,
    readme_content: Optional[str] = None,
    projects_dir: Optional[Path] = None,
) -> Path:
    """Store project README and metadata in the dedicated data/projects/ directory."""
    target_dir = projects_dir or PROJECTS_DIR
    slug = slugify(project.title)
    project_folder = target_dir / slug
    project_folder.mkdir(parents=True, exist_ok=True)

    # 1. Save README.md
    readme_path = project_folder / "README.md"
    readme_text = readme_content or f"# {project.title}\n\n{project.description}\n\n## Tech Stack\n{', '.join(project.tech_stack)}"
    readme_path.write_text(readme_text, encoding="utf-8")

    # 2. Save project.json metadata
    meta_path = project_folder / "project.json"
    meta_path.write_text(project.model_dump_json(indent=2), encoding="utf-8")

    logger.info(f"Persisted project knowledge base to: {project_folder}")
    return project_folder


def ingest_github_project(
    url_or_text: str,
    client: Optional[genai.Client] = None,
    raw_readme_override: Optional[str] = None,
    projects_dir: Optional[Path] = None,
    profile_path: Optional[Path] = None,
) -> Tuple[ProjectItem, List[str], Path]:
    """Ingest a project from GitHub URL, extract deep tech details, and sync to profile.
    
    Returns:
        (ProjectItem, newly_added_skills: List[str], saved_folder_path: Path)
    """
    coords = extract_github_repo_coords(url_or_text)
    owner, repo, github_url = coords if coords else ("unknown", "repo", None)

    # 1. Fetch README
    readme_content = raw_readme_override
    if not readme_content and coords:
        readme_content = fetch_github_readme(owner, repo)

    if not readme_content:
        # Fall back to raw text if no remote README found
        readme_content = url_or_text

    # 2. Extract structured ProjectItem via Gemini
    ai_client = client or get_genai_client()
    prompt = (
        f"Analyze this GitHub project README and extract full technical specifications:\n"
        f"Repository URL: {github_url or 'None'}\n\n"
        f"--- README CONTENT ---\n{readme_content[:12000]}"
    )

    response = generate_content_with_fallback(
        client=ai_client,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=README_ANALYZER_SYSTEM_INSTRUCTION,
            response_mime_type="application/json",
            response_schema=ProjectItem,
            temperature=0.1,
        ),
    )

    project = ProjectItem.model_validate_json(response.text)
    if github_url and not project.github_url:
        project.github_url = github_url

    # 3. Save to dedicated projects/ directory
    saved_folder = save_project_to_knowledge_base(project, readme_content, projects_dir)

    # 4. Sync into candidate profile and add new verified skills
    _, new_skills = add_project_to_profile(project, file_path=profile_path)

    return project, new_skills, saved_folder


def get_all_stored_projects(projects_dir: Optional[Path] = None) -> List[Dict]:
    """Retrieve all projects and READMEs from the dedicated knowledge base."""
    target_dir = projects_dir or PROJECTS_DIR
    if not target_dir.exists():
        return []

    results = []
    for folder in target_dir.iterdir():
        if folder.is_dir():
            meta_path = folder / "project.json"
            readme_path = folder / "README.md"
            if meta_path.exists():
                try:
                    data = json.loads(meta_path.read_text(encoding="utf-8"))
                    readme_text = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""
                    results.append({
                        "meta": data,
                        "readme": readme_text,
                        "folder": folder,
                    })
                except Exception as exc:
                    logger.warning(f"Error reading project from {folder}: {exc}")
    return results
