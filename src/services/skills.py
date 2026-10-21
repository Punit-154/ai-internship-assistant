"""Skill Normalization & Semantic Alias Matching.

Solves the acronym blindness problem in keyword matching:
- "DRF" <-> "Django REST Framework"
- "K8s" <-> "Kubernetes"
- "LLM" <-> "Large Language Models"
- "AWS" <-> "Amazon Web Services"
etc.

Also fixes naive substring false positives: "C" no longer matches inside
"C++", "SQL" no longer matches "SQLite", "Java" no longer matches "JavaScript".
"""

import re
from typing import Iterable, Optional

# Normalized alias -> canonical skill name. Matching normalizes both sides
# and resolves aliases before comparing, so any member of a group matches
# any other member.
_SKILL_ALIASES: dict[str, str] = {
    # Django
    "drf": "django rest framework",
    "djangorestframework": "django rest framework",
    "django restframework": "django rest framework",
    # Frontend
    "js": "javascript",
    "ts": "typescript",
    "nextjs": "next.js",
    "reactjs": "react",
    "react.js": "react",
    "vuejs": "vue",
    "nodejs": "node.js",
    "node": "node.js",
    "html5": "html",
    "css3": "css",
    "sass": "scss",
    # Cloud & DevOps
    "aws": "amazon web services",
    "amazon web services aws": "amazon web services",
    "gcp": "google cloud",
    "google cloud platform": "google cloud",
    "k8s": "kubernetes",
    "cicd": "ci/cd",
    "ci cd": "ci/cd",
    # AI / ML
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "genai": "generative ai",
    "gen ai": "generative ai",
    "llm": "large language models",
    "llms": "large language models",
    "rag": "retrieval-augmented generation",
    # CS fundamentals
    "dsa": "data structures and algorithms",
    "oop": "object-oriented programming",
    "object oriented programming": "object-oriented programming",
    "rest api": "restful apis",
    "rest apis": "restful apis",
    "restful api": "restful apis",
    "rest": "restful apis",
    "restful": "restful apis",
    # Databases
    "postgres": "postgresql",
    "mongo": "mongodb",
    "ms sql": "sql server",
    "mssql": "sql server",
    # Languages
    "cpp": "c++",
    "c plus plus": "c++",
    "csharp": "c#",
    "c sharp": "c#",
    "golang": "go",
    "python3": "python",
}

# Skill pairs that must NEVER be treated as equal via substring containment.
_ANTI_CONTAINMENT_PAIRS: set[tuple[str, str]] = {
    ("c", "c++"),
    ("c", "c#"),
    ("java", "javascript"),
    ("git", "github"),
}


def normalize_skill(text: str) -> str:
    """Lowercase and strip punctuation (except #, +, ., /, -) from a skill name."""
    if not text:
        return ""
    cleaned = re.sub(r"[^a-z0-9+#./\- ]+", " ", text.lower())
    return " ".join(cleaned.split())


def canonical_skill(text: str) -> str:
    """Normalize a skill and resolve it to its canonical form via the alias map."""
    norm = normalize_skill(text)
    if not norm:
        return ""
    return _SKILL_ALIASES.get(norm, norm)


def _word_contains(needle: str, haystack: str) -> bool:
    """True if needle appears in haystack as a whole word (boundary-guarded)."""
    if not needle or not haystack:
        return False
    pattern = rf"(?<![a-z0-9+#]){re.escape(needle)}(?![a-z0-9+#])"
    return re.search(pattern, haystack) is not None


def skill_group(text: str) -> set[str]:
    """Set of all normalized names considered equivalent to `text`.

    Includes the normalized form, its alias-canonicalized form, and the
    canonicalization of each individual word (so "Golang FastAPI" yields "go").
    """
    norm = normalize_skill(text)
    if not norm:
        return set()
    group = {norm, _SKILL_ALIASES.get(norm, norm)}
    for word in norm.split():
        if len(word) >= 2:
            group.add(_SKILL_ALIASES.get(word, word))
    return group


def match_skill(skill_a: str, skill_b: str) -> bool:
    """Determine whether two skill names refer to the same capability.

    Matching strategy (in order):
    1. Equivalence-group intersection after normalization + alias resolution.
    2. Bidirectional whole-word containment (only for tokens with length >= 4,
       and explicitly refused for known anti-pairs such as C vs C++).
    """
    a = canonical_skill(skill_a)
    b = canonical_skill(skill_b)
    if not a or not b:
        return False
    if a == b:
        return True

    if skill_group(skill_a) & skill_group(skill_b):
        return True

    pair = (a, b) if a <= b else (b, a)
    if pair in _ANTI_CONTAINMENT_PAIRS:
        return False

    if len(a) >= 4 and (_word_contains(a, b) or _word_contains(b, a)):
        return True
    return False


def find_matching_skill(required: str, candidate_pool: Iterable[str]) -> Optional[str]:
    """Return the first candidate in the pool equivalent to `required`, else None."""
    for candidate in candidate_pool:
        if match_skill(required, candidate):
            return candidate
    return None
