"""Prospeo Search-Person Adapter (P1.2 Automated Recruiter Discovery).

Turns a company name or careers domain into a short list of recruiter /
talent-acquisition people with verified emails available, via Prospeo's
"Search Person" REST API. Feeds candidates into the existing approved
enrichment → cold-outreach flow. Shares the prospeo economics guardrails:

- Free plan pricing: 1 credit per search page that returns at least one
  person (25 profiles/page, no emails revealed). Repeating the exact same
  page within 30 days is free (``"free": true`` → 0 credits charged).
- Results never contain a revealed email; each candidate is enriched
  afterwards with Enrich-Person (1 credit, verified-email-only). We filter
  server-side with ``person_contact_details.email=["VERIFIED"]`` so every
  candidate shown is enrichable; if the filter is plan-restricted, the
  adapter automatically retries once without it (failed search responses
  cost 0 credits).
- Free-tier-safe filters only: ``person_job_title`` (CONTAINS include list),
  ``company`` (websites/names), ``person_contact_details``. Filters marked
  Starter/Growth+ in the docs (education, job_change, past_*, revenue,
  funding) are never sent.
- Search rate limits are lower than enrich — self-throttled at
  PROSPEO_SEARCH_MIN_INTERVAL_SECONDS (default 2s) via the shared throttle
  clock, plus the bot layer's rolling-24h ``PROSPEO_MAX_SEARCHES_PER_DAY``
  budget.

Endpoint (verified against https://prospeo.io/api-docs, June 2026):
- POST https://api.prospeo.io/search-person
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import httpx

from src.config import settings
from src.services.contact_enrichment import (
    CODE_BAD_RESPONSE,
    CODE_INSUFFICIENT_CREDITS,
    CODE_INVALID_API_KEY,
    CODE_RATE_LIMITED,
    ProspeoError,
    _post_prospeo,
    _rate_limit_message,
    _throttle,
    validate_prospero_api_key,
)

logger = logging.getLogger(__name__)

PROSPEO_API_BASE = "https://api.prospeo.io"
ENDPOINT_SEARCH_PERSON = f"{PROSPEO_API_BASE}/search-person"

# Search-specific error codes (enrich codes live in contact_enrichment).
CODE_NO_RESULTS = "NO_RESULTS"
CODE_INVALID_FILTERS = "INVALID_FILTERS"
CODE_PLAN_REQUIRED = "PLAN_REQUIRED"
CODE_SERVICE_TEMPORARILY_UNAVAILABLE = "SERVICE_TEMPORARILY_UNAVAILABLE"

SEARCH_MESSAGE_BY_CODE = {
    CODE_NO_RESULTS: (
        "No recruiter profiles matched at this company. Try the raw domain "
        "(e.g. /prospect stripe.com), a broader keyword (e.g. /prospect Stripe | hr), "
        "or add contacts by LinkedIn URL instead."
    ),
    CODE_INVALID_FILTERS: "Prospeo rejected the search filters.",
    CODE_PLAN_REQUIRED: "This search filter is not available on the current Prospeo plan.",
    CODE_SERVICE_TEMPORARILY_UNAVAILABLE: "Prospeo search is briefly unavailable — try again shortly.",
    CODE_INSUFFICIENT_CREDITS: (
        "Prospeo monthly credits exhausted. Check /prospeo for the renewal date, or swap in a "
        "fresh PROSPEO_API_KEY in .env and restart the bot."
    ),
    CODE_INVALID_API_KEY: (
        "Prospeo rejected the API key. Paste the current key from your Prospeo dashboard into "
        "PROSPEO_API_KEY in .env and restart the bot."
    ),
}

# CONTAINS-matched title hints for recruiters / hiring-side people. Deliberately
# substring-safe (no bare "hr"/"talent" — those match names like "Christopher").
DEFAULT_RECRUITER_TITLES: Tuple[str, ...] = (
    "recruiter",
    "recruitment",
    "talent acquisition",
    "hiring manager",
    "campus recruiting",
    "university relations",
    "people operations",
)

_VERIFIED_EMAIL_FILTER = {"person_contact_details": {"email": ["VERIFIED"]}}

__all__ = [
    "DEFAULT_RECRUITER_TITLES",
    "ENDPOINT_SEARCH_PERSON",
    "PROSPEO_API_BASE",
    "ProspectCandidate",
    "ProspectPage",
    "search_recruiters",
    "enrich_prospect",
]


@dataclass
class ProspectCandidate:
    """One search result worth enriching (surfaced as a /prospect button)."""
    person_id: Optional[str] = None
    full_name: str = ""
    role_title: Optional[str] = None
    linkedin_url: Optional[str] = None
    company_name: Optional[str] = None
    company_domain: Optional[str] = None


@dataclass
class ProspectPage:
    """Result of one /search-person call (page 1 by default)."""
    candidates: List[ProspectCandidate] = field(default_factory=list)
    total_count: int = 0
    total_pages: int = 1
    current_page: int = 1
    free: bool = False  # dedupe repeat within 30 days → 0 credits
    credits_used: int = 1
    verified_only_effective: bool = True


def _company_filter(company: str) -> Dict:
    """Prospeo ``company`` filter: URL or domain → websites, bare text → names."""
    text = (company or "").strip()
    if not text:
        raise ValueError("Provide a company name or careers domain (e.g. 'Stripe' or 'stripe.com').")
    hostish = text.lower().replace("https://", "").replace("http://", "").split("/")[0].split("?")[0].strip(".")
    if hostish and "." in hostish and " " not in hostish:
        domain = hostish[4:] if hostish.startswith("www.") else hostish
        return {"websites": {"include": [domain]}}
    return {"names": {"include": [text]}}


def _build_search_filters(
    company: str,
    keyword: Optional[str] = None,
    verified_only: bool = True,
) -> Dict:
    """Assemble the free-tier-safe filter set for /search-person."""
    titles = [keyword.strip()] if (keyword and keyword.strip()) else list(DEFAULT_RECRUITER_TITLES)
    filters: Dict = {
        "company": _company_filter(company),
        "person_job_title": {"include": titles, "match_mode": "CONTAINS"},
    }
    if verified_only:
        filters.update(_VERIFIED_EMAIL_FILTER)
    return filters


def _raise_search_error(error_code: str, filter_error: Optional[str]) -> ProspeoError:
    message = SEARCH_MESSAGE_BY_CODE.get(error_code, f"Prospeo search failed ({error_code}).")
    if filter_error:
        message = f"{message} ({str(filter_error)[:160]})"
    return ProspeoError(message, code=error_code, filter_error=filter_error or None)


def _search_error_and_filter(body: Dict) -> Tuple[str, Optional[str]]:
    error_code = str(body.get("error_code") or "UNKNOWN")
    filter_error = body.get("filter_error")
    return error_code, (str(filter_error) if filter_error else None)


def _parse_search_response(resp: httpx.Response) -> ProspectPage:
    """Branch on the /search-person contract: 429 first, then the JSON error flag."""
    if resp.status_code == 429:
        raise ProspeoError(_rate_limit_message(resp, resource="search"), code=CODE_RATE_LIMITED)

    try:
        body = resp.json()
    except ValueError as exc:
        raise ProspeoError(
            f"Prospeo returned a non-JSON body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE
        ) from exc
    if not isinstance(body, dict):
        raise ProspeoError(f"Prospeo returned an unexpected body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE)

    if body.get("error") or body.get("error_code"):
        error_code, filter_error = _search_error_and_filter(body)
        raise _raise_search_error(error_code, filter_error)
    if resp.status_code >= 400:
        raise ProspeoError(f"Prospeo search failed with HTTP {resp.status_code}.", code=CODE_BAD_RESPONSE)

    candidates: List[ProspectCandidate] = []
    for item in body.get("results") or []:
        if not isinstance(item, dict):
            continue
        person = item.get("person") or {}
        company = item.get("company") or {}
        first = (person.get("first_name") or "").strip()
        last = (person.get("last_name") or "").strip()
        full_name = (person.get("full_name") or f"{first} {last}").strip()
        candidates.append(
            ProspectCandidate(
                person_id=(str(person.get("person_id")) if person.get("person_id") else None),
                full_name=full_name,
                role_title=person.get("current_job_title"),
                linkedin_url=person.get("linkedin_url"),
                company_name=(company.get("name") if isinstance(company, dict) else None) or None,
                company_domain=(
                    (company.get("domain") or company.get("website") or "") if isinstance(company, dict) else ""
                ).strip() or None,
            )
        )

    pagination = body.get("pagination") or {}
    free = bool(body.get("free"))
    return ProspectPage(
        candidates=candidates,
        total_count=int(pagination.get("total_count") or len(candidates)),
        total_pages=int(pagination.get("total_page") or 1),
        current_page=int(pagination.get("current_page") or 1),
        free=free,
        credits_used=0 if (free or not candidates) else 1,
        verified_only_effective=True,
    )


def search_recruiters(
    company: str,
    keyword: Optional[str] = None,
    page: int = 1,
    api_key: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
    timeout: float = 10.0,
) -> ProspectPage:
    """One /search-person page of recruiter-type people at a company (1 credit max).

    Sends only free-plan filters, defaulting to verified-email-available people.
    If Prospeo answers PLAN_REQUIRED/INVALID_FILTERS naming the verified-email
    filter, retries once without it (failed searches return no results → 0 credits).
    """
    key = validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
    headers = {"X-KEY": key, "Content-Type": "application/json"}
    _throttle(min_interval=settings.PROSPEO_SEARCH_MIN_INTERVAL_SECONDS)

    verified_only = bool(settings.PROSPEO_SEARCH_VERIFIED_ONLY)
    payload = {
        "page": max(1, int(page)),
        "filters": _build_search_filters(company, keyword=keyword, verified_only=verified_only),
    }

    resp = _post_prospeo(ENDPOINT_SEARCH_PERSON, payload, headers, http_client, timeout)

    retry_needed = False
    body_peek = None
    if resp.status_code != 429:
        try:
            body_peek = resp.json()
        except ValueError:
            body_peek = None
        if isinstance(body_peek, dict) and (body_peek.get("error") or body_peek.get("error_code")):
            error_code, filter_error = _search_error_and_filter(body_peek)
            if verified_only and error_code in (CODE_PLAN_REQUIRED, CODE_INVALID_FILTERS):
                filter_text = (filter_error or "").lower()
                if not filter_text or "contact_details" in filter_text or "contact_details" in error_code.lower():
                    retry_needed = True

    if retry_needed:
        logger.info(
            "Prospeo rejected the verified-email filter (%s) — retrying without it", body_peek.get("error_code")
        )
        _throttle(min_interval=settings.PROSPEO_SEARCH_MIN_INTERVAL_SECONDS)
        second_payload = {
            "page": max(1, int(page)),
            "filters": _build_search_filters(company, keyword=keyword, verified_only=False),
        }
        resp = _post_prospeo(ENDPOINT_SEARCH_PERSON, second_payload, headers, http_client, timeout)
        parsed = _parse_search_response(resp)
        parsed.verified_only_effective = False
        return parsed

    return _parse_search_response(resp)


def enrich_prospect(
    candidate: ProspectCandidate,
    api_key: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
    timeout: float = 10.0,
):
    """Resolve the verified email for a search candidate (1 credit, dedupe-aware).

    Prefers the exact ``person_id`` from Search-Person; falls back to the
    candidate's LinkedIn URL when the search result omitted an id.
    """
    from src.services.contact_enrichment import enrich_person

    if candidate.person_id:
        return enrich_person(person_id=candidate.person_id, api_key=api_key, http_client=http_client, timeout=timeout)
    if candidate.linkedin_url:
        return enrich_person(linkedin_url=candidate.linkedin_url, api_key=api_key, http_client=http_client, timeout=timeout)
    raise ValueError("Candidate has neither a person_id nor a LinkedIn URL to enrich.")
