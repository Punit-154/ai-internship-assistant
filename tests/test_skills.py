"""Tests for Skill Normalization & Alias Matching."""

from src.services.skills import (
    canonical_skill,
    find_matching_skill,
    match_skill,
    normalize_skill,
)


def test_acronym_alias_resolution():
    assert match_skill("DRF", "Django REST Framework") is True
    assert match_skill("django rest framework", "DRF") is True
    assert match_skill("K8s", "Kubernetes") is True
    assert match_skill("LLM", "Large Language Models") is True
    assert match_skill("AWS", "Amazon Web Services (AWS)") is True
    assert match_skill("Amazon Web Services", "aws") is True
    assert match_skill("GCP", "Google Cloud") is True
    assert match_skill("ML", "Machine Learning") is True
    assert match_skill("GenAI", "Generative AI") is True
    assert match_skill("RAG", "Retrieval-Augmented Generation (RAG)") is True
    assert match_skill("DSA", "Data Structures and Algorithms") is True
    assert match_skill("OOP", "Object-Oriented Programming") is True
    assert match_skill("CI/CD", "CICD") is True
    assert match_skill("Postgres", "PostgreSQL") is True
    assert match_skill("C++", "cpp") is True
    assert match_skill("C#", "csharp") is True
    assert match_skill("Go", "Golang") is True
    assert match_skill("NextJS", "Next.js") is True
    assert match_skill("NodeJS", "Node.js") is True


def test_anti_containment_pairs_never_match():
    """The old naive substring logic wrongly matched these; they must stay distinct."""
    assert match_skill("C", "C++") is False
    assert match_skill("C", "C#") is False
    assert match_skill("Java", "JavaScript") is False
    assert match_skill("Git", "GitHub") is False


def test_word_boundary_containment():
    """Longer names still match their variants via whole-word containment."""
    assert match_skill("Data Structures", "Data Structures and Algorithms") is True
    assert match_skill("Spring", "Spring Boot") is True
    assert match_skill("Docker", "Docker Compose") is True
    assert match_skill("REST", "RESTful APIs") is True


def test_substring_false_positive_fixed():
    assert match_skill("SQL", "SQLite") is False
    assert match_skill("Python", "Pydantic") is False
    assert match_skill("Go", "Golang FastAPI") is True


def test_exact_match_case_insensitive():
    assert match_skill("PYTHON", "python") is True
    assert match_skill("FastAPI", "FastAPI") is True


def test_unrelated_skills_never_match():
    assert match_skill("Python", "Java") is False
    assert match_skill("Kafka", "Rust") is False
    assert match_skill("Docker", "Kubernetes") is False


def test_empty_inputs_are_safe():
    assert match_skill("", "Python") is False
    assert match_skill("Python", "") is False
    assert match_skill(None, "Python") is False  # type: ignore[arg-type]
    assert normalize_skill("  Kafka!  ") == "kafka"


def test_canonical_skill_resolution():
    assert canonical_skill("DRF") == "django rest framework"
    assert canonical_skill("Azure") == "azure"
    assert canonical_skill("Amazon Web Services (AWS)") == "amazon web services"


def test_find_matching_skill_returns_pool_member():
    pool = ["Python", "Amazon Web Services (AWS)", "Kobe?"]
    assert find_matching_skill("aws", pool) == "Amazon Web Services (AWS)"
    assert find_matching_skill("Rust", pool) is None
