"""Alumni & Engineer Contact Discovery (P1 Outreach).

Two discovery modes, both human-verification-gated:

1. GitHub public API sweep — finds engineers at a target company by their
   public profile company field. Read-only, unauthenticated (60 req/h) unless
   GITHUB_TOKEN is set. Public data only; no scraping, no ToS violations.

2. LinkedIn search URL prefills — the system generates exact LinkedIn search
   URLs (recruiters at company, VIT alumni at company) for the user to open
   and shortlist by hand. LinkedIn collection happens by the human copying a
   profile URL into /addcontact, never by automated scraping.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlencode as _urlencode

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_MAX_RESULTS = 10
GITHUB_TIMEOUT = 10.0

CANDIDATE_SCHOOL = "Vellore Institute of Technology"


class AlumniFinderError(RuntimeError):
    """Raised on GitHub API failures (rate limited, blocked, malformed)."""


@dataclass
class GitHubContact:
    """Public GitHub profile match for a target company."""
    login: str = ""
    full_name: str = ""
    company: Optional[str] = None
    email: Optional[str] = None
    github_url: str = ""
    blog: Optional[str] = None
    bio_matches: List[str] = field(default_factory=list)


def github_headers() -> Dict[str, str]:
    """Accept header + optional token authorization (raises rate limit when set)."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AI-Internship-Assistant/1.0"}
    if settings.GITHUB_TOKEN and not settings.GITHUB_TOKEN.startswith("ghp_placeholder"):
        headers["Authorization"] = f"Bearer {settings.GITHUB_TOKEN}"
    return headers


def find_engineers_on_github(company_name: str, http_client: Optional[httpx.Client] = None) -> List[GitHubContact]:
    """Public GitHub users whose profile company mentions the target company."""
    query = f'{company_name.strip()} in:company type:users'
    try:
        if http_client is not None:
            resp = http_client.get(
                f"{GITHUB_API_BASE}/search/users",
                params={"q": query, "per_page": GITHUB_MAX_RESULTS},
                headers=github_headers(),
                timeout=GITHUB_TIMEOUT,
            )
        else:
            with httpx.Client(timeout=GITHUB_TIMEOUT) as fresh:
                resp = fresh.get(
                    f"{GITHUB_API_BASE}/search/users",
                    params={"q": query, "per_page": GITHUB_MAX_RESULTS},
                    headers=github_headers(),
                    timeout=GITHUB_TIMEOUT,
                )
    except httpx.HTTPError as exc:
        raise AlumniFinderError(f"GitHub search failed: {exc}") from exc

    if resp.status_code == 403:
        raise AlumniFinderError("GitHub rate limit hit (60 req/h unauthenticated). Add GITHUB_TOKEN in .env to raise it.")
    if resp.status_code >= 400:
        raise AlumniFinderError(f"GitHub search returned HTTP {resp.status_code}.")

    items = resp.json().get("items", [])
    contacts: List[GitHubContact] = []
    for item in items[:GITHUB_MAX_RESULTS]:
        detail_resp = _fetch_github_user(item.get("login", ""), http_client)
        if detail_resp is None:
            continue
        contacts.append(detail_resp)
    return contacts


def _fetch_github_user(login: str, http_client: Optional[httpx.Client] = None) -> Optional[GitHubContact]:
    """Fetch one public profile (filters out logins with an empty company field)."""
    if not login:
        return None
    try:
        if http_client is not None:
            resp = http_client.get(f"{GITHUB_API_BASE}/users/{login}", headers=github_headers(), timeout=GITHUB_TIMEOUT)
        else:
            with httpx.Client(timeout=GITHUB_TIMEOUT) as fresh:
                resp = fresh.get(f"{GITHUB_API_BASE}/users/{login}", headers=github_headers(), timeout=GITHUB_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning(f"GitHub user fetch failed for {login}: {exc}")
        return None
    if resp.status_code >= 400:
        return None
    data = resp.json()

    company = (data.get("company") or "").strip().strip("@") or None
    if not company:
        return None
    blog = (data.get("blog") or "").strip() or None
    email = (data.get("email") or "").strip() or None
    bio = (data.get("bio") or "").lower()
    bio_matches = [k for k in ("intern", "recruit", "hiring", "recruiter") if k in bio]

    return GitHubContact(
        login=login,
        full_name=(data.get("name") or "").strip(),
        company=company,
        email=email,
        github_url=data.get("html_url") or f"https://github.com/{login}",
        blog=blog,
        bio_matches=bio_matches,
    )


def build_linkedin_search_urls(company_name: str, role_keywords: str = "recruiter OR talent") -> Dict[str, str]:
    """Prefilled LinkedIn search URLs the candidate opens and shortlists manually."""
    recruiter_url = "https://www.linkedin.com/search/results/people/?" + _urlencode({
        "keywords": f'{role_keywords} "{company_name}"',
        "origin": "GLOBAL_SEARCH_HEADER",
    })
    alumni_url = "https://www.linkedin.com/search/results/people/?" + _urlencode({
        "keywords": f'"{CANDIDATE_SCHOOL}" "{company_name}"',
        "schoolFilter": CANDIDATE_SCHOOL,
        "origin": "GLOBAL_SEARCH_HEADER",
    })
    return {
        "recruiters": recruiter_url,
        "alumni": alumni_url,
        "engineers": "https://www.linkedin.com/search/results/people/?" + _urlencode({
            "keywords": f'engineer "{company_name}"',
            "origin": "GLOBAL_SEARCH_HEADER",
        }),
    }
