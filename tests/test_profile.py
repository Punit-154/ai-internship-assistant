"""Tests for Candidate Profile Management, Intelligent Job Matching, and Project Ingestion."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.models.candidate import CandidateProfile, ProjectItem
from src.models.job import EligibilityStatus
from src.services.profile import (
    add_project_to_profile,
    evaluate_job_match,
    get_candidate_profile,
    parse_project_from_text,
    save_candidate_profile,
    update_candidate_profile,
)


def test_profile_defaults():
    """Verify default values in CandidateProfile reflect 3rd year status."""
    p = CandidateProfile()
    assert "Punit" in p.full_name
    assert "VIT" in p.college
    assert p.graduation_year == 2028
    assert p.current_college_year == 3
    assert "Bangalore" in p.summer_locations
    assert "Chennai" in p.summer_locations
    assert "Hyderabad" in p.summer_locations
    assert "Python" in p.verified_skills


def test_save_and_update_profile(tmp_path: Path):
    """Test updating and persisting profile modifications to disk."""
    test_file = tmp_path / "test_profile.json"
    p = CandidateProfile(graduation_year=2028, summer_locations=["Bangalore", "Pune"])
    save_candidate_profile(p, test_file)

    loaded = get_candidate_profile(test_file)
    assert loaded.summer_locations == ["Bangalore", "Pune"]

    updated = update_candidate_profile({"current_college_year": 3}, test_file)
    assert updated.current_college_year == 3

    refetched = get_candidate_profile(test_file)
    assert refetched.current_college_year == 3


def test_update_invalid_field_raises_key_error(tmp_path: Path):
    """Verify updating a non-existent field raises KeyError."""
    test_file = tmp_path / "test_profile.json"
    with pytest.raises(KeyError, match="Unknown profile field"):
        update_candidate_profile({"non_existent_field": 123}, test_file)


def test_match_remote_semester_job():
    """Verify remote opportunities match current semester availability."""
    p = CandidateProfile(current_availability="Remote", graduation_year=2028)
    evaluation = evaluate_job_match(
        role_title="Backend Engineering Intern",
        locations=["Remote"],
        allowed_grad_years=[2028],
        required_skills=["Python", "FastAPI"],
        profile=p,
    )
    assert evaluation.batch_status == EligibilityStatus.ELIGIBLE
    assert evaluation.role_match is True
    assert "Remote / Virtual" in evaluation.location_fit
    assert "Python" in evaluation.matching_skills
    assert evaluation.overall_match_score >= 0.85


def test_match_summer_hub_location():
    """Verify Bangalore/Chennai/Hyderabad locations are recognized as summer hubs."""
    p = CandidateProfile(graduation_year=2028, summer_locations=["Bangalore", "Chennai", "Hyderabad"])
    evaluation = evaluate_job_match(
        role_title="Cloud & AI Intern",
        locations=["Bengaluru / Bangalore"],
        allowed_grad_years=[2027, 2028],
        required_skills=["AWS", "Python"],
        profile=p,
    )
    assert evaluation.batch_status == EligibilityStatus.ELIGIBLE
    assert evaluation.location_is_viable is True
    assert "Summer Hub" in evaluation.location_fit


def test_match_skill_gaps_identification():
    """Verify required skills are split into matching and gap lists."""
    p = CandidateProfile(verified_skills=["Python", "SQL", "Git"])
    evaluation = evaluate_job_match(
        role_title="Data Engineer Intern",
        locations=["Remote"],
        allowed_grad_years=[2028],
        required_skills=["Python", "Kafka", "Rust"],
        profile=p,
    )
    assert "Python" in evaluation.matching_skills
    assert "Kafka" in evaluation.missing_skills
    assert "Rust" in evaluation.missing_skills


def test_parse_project_from_text_mocked():
    """Test Gemini structured extraction of a project from informal text."""
    mock_payload = {
        "title": "CloudSentinel Log Monitor",
        "tech_stack": ["Python", "FastAPI", "AWS", "Docker"],
        "description": "Real-time AWS CloudWatch anomaly detection and alerting daemon.",
        "github_url": "https://github.com/punit/cloud-sentinel",
        "live_demo_url": None,
    }
    mock_response = MagicMock()
    mock_response.text = json.dumps(mock_payload)

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_response

    project = parse_project_from_text(
        text="I built CloudSentinel with Python and FastAPI on AWS and Docker to detect log anomalies.",
        client=mock_client,
    )
    assert project.title == "CloudSentinel Log Monitor"
    assert "Docker" in project.tech_stack
    assert project.github_url == "https://github.com/punit/cloud-sentinel"


def test_add_project_to_profile_auto_adds_skills(tmp_path: Path):
    """Test that adding a project automatically updates verified_skills with new technologies."""
    test_file = tmp_path / "test_profile.json"
    p = CandidateProfile(verified_skills=["Python", "SQL"])
    save_candidate_profile(p, test_file)

    new_project = ProjectItem(
        title="Distributed Queue Worker",
        tech_stack=["Python", "Redis", "Docker", "RabbitMQ"],
        description="High throughput asynchronous worker pool.",
    )

    updated_profile, new_skills = add_project_to_profile(new_project, test_file)

    assert len(updated_profile.verified_projects) == 1
    assert updated_profile.verified_projects[0].title == "Distributed Queue Worker"
    # Python was already verified; Redis, Docker, RabbitMQ should be newly added
    assert set(new_skills) == {"Redis", "Docker", "RabbitMQ"}
    assert "Redis" in updated_profile.verified_skills
    assert "Docker" in updated_profile.verified_skills
    assert "RabbitMQ" in updated_profile.verified_skills


def test_add_project_to_profile_updates_existing_project(tmp_path: Path):
    """Test that adding a project with an existing title or github_url updates it in place."""
    test_file = tmp_path / "test_profile.json"
    p = CandidateProfile(
        verified_skills=["Python", "FastAPI"],
        verified_projects=[
            ProjectItem(
                title="Reco.ai",
                tech_stack=["Python", "FastAPI"],
                description="Initial version",
                github_url="https://github.com/Punit-154/Reco.ai/",
            )
        ],
    )
    save_candidate_profile(p, test_file)

    # 1. Update by matching github_url (different case / trailing slash)
    updated_project = ProjectItem(
        title="Reco.ai - AI Finance Reconciliation",
        tech_stack=["Python", "FastAPI", "PostgreSQL", "Groq"],
        description="V2 reconciliation engine with human audit trail.",
        github_url="https://github.com/punit-154/reco.ai",
    )
    updated_profile, new_skills = add_project_to_profile(updated_project, test_file)


    assert len(updated_profile.verified_projects) == 1
    assert updated_profile.verified_projects[0].title == "Reco.ai - AI Finance Reconciliation"
    assert updated_profile.verified_projects[0].description == "V2 reconciliation engine with human audit trail."
    assert set(new_skills) == {"PostgreSQL", "Groq"}
    assert "PostgreSQL" in updated_profile.verified_skills


    # 2. Update by matching title when github_url is not set
    p2 = ProjectItem(
        title="Reco.ai - AI Finance Reconciliation",
        tech_stack=["Python", "FastAPI", "PostgreSQL", "Groq", "Llama 3.3"],
        description="V3 with Llama 3.3 support.",
    )
    updated_profile2, new_skills2 = add_project_to_profile(p2, test_file)
    assert len(updated_profile2.verified_projects) == 1
    assert updated_profile2.verified_projects[0].description == "V3 with Llama 3.3 support."
    assert "Llama 3.3" in new_skills2
