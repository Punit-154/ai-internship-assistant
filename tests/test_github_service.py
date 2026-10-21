"""Tests for GitHub Project Ingestion and Knowledge Base Service."""

import json
from pathlib import Path
from unittest.mock import MagicMock
from src.models.candidate import ProjectItem
from src.services.github_service import (
    extract_github_repo_coords,
    get_all_stored_projects,
    ingest_github_project,
    save_project_to_knowledge_base,
    slugify,
)


def test_extract_github_repo_coords():
    """Verify regex parsing of various GitHub repository URL formats."""
    url1 = "https://github.com/punit-developer/distributed-queue"
    coords1 = extract_github_repo_coords(url1)
    assert coords1 is not None
    assert coords1[0] == "punit-developer"
    assert coords1[1] == "distributed-queue"
    assert coords1[2] == "https://github.com/punit-developer/distributed-queue"

    url2 = "https://www.github.com/octocat/Spoon-Knife.git"
    coords2 = extract_github_repo_coords(url2)
    assert coords2 is not None
    assert coords2[0] == "octocat"
    assert coords2[1] == "Spoon-Knife"

    invalid_text = "Just a text mentioning github without link"
    assert extract_github_repo_coords(invalid_text) is None


def test_slugify():
    assert slugify("Distributed Task Queue & Scheduler!") == "distributed-task-queue-scheduler"
    assert slugify("AI / ML Pipeline") == "ai-ml-pipeline"


def test_save_and_retrieve_projects_knowledge_base(tmp_path: Path):
    """Verify README and metadata are stored in data/projects/<slug>/ and retrievable."""
    project = ProjectItem(
        title="Cloud Sentinel Anomaly Detector",
        tech_stack=["Python", "FastAPI", "Docker", "AWS"],
        description="Real-time log anomaly detector analyzing cloud logs.",
        github_url="https://github.com/punit/cloud-sentinel",
    )
    readme = "# Cloud Sentinel\n\nReal-time telemetry monitor."

    folder = save_project_to_knowledge_base(project, readme_content=readme, projects_dir=tmp_path)
    assert folder.exists()
    assert (folder / "README.md").exists()
    assert (folder / "project.json").exists()
    assert (folder / "README.md").read_text(encoding="utf-8") == readme

    all_stored = get_all_stored_projects(projects_dir=tmp_path)
    assert len(all_stored) == 1
    assert all_stored[0]["meta"]["title"] == "Cloud Sentinel Anomaly Detector"
    assert all_stored[0]["readme"] == readme


def test_ingest_github_project_mocked(tmp_path: Path):
    """Verify end-to-end ingestion with mocked LLM and raw README override."""
    mock_item = ProjectItem(
        title="Distributed Log Processor",
        tech_stack=["Go", "Kafka", "Docker", "PostgreSQL"],
        description="High-throughput stream processing engine handling 50k logs/sec.",
        github_url="https://github.com/punit/log-processor",
    )

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = MagicMock(
        text=mock_item.model_dump_json()
    )

    test_prof_file = tmp_path / "test_candidate_profile.json"
    project, new_skills, folder = ingest_github_project(
        url_or_text="https://github.com/punit/log-processor",
        client=mock_client,
        raw_readme_override="# Log Processor\nBuilt with Go and Kafka.",
        projects_dir=tmp_path,
        profile_path=test_prof_file,
    )

    assert project.title == "Distributed Log Processor"
    assert "Go" in project.tech_stack
    assert "Kafka" in project.tech_stack
    assert folder.exists()
    assert (folder / "README.md").exists()
