"""Proactive Opportunity Discovery Engine (Tier 1 Roadmap Item #3).

Turns the assistant from push-only into pull-based ingestion by polling
company career/ATS boards the candidate explicitly subscribes to
(`watch_targets` table):

- Greenhouse:  GET boards-api.greenhouse.io/v1/boards/{token}/jobs   (public JSON)
- Lever:       GET api.lever.co/v0/postings/{slug}?mode=json         (public JSON)
- Ashby:       GET api.ashbyhq.com/posting-api/job-board/{slug}      (public JSON)
- CUSTOM_URL:  scrape_url + one batched Gemini list-extraction

Structured ATS JSON is parsed WITHOUT any LLM call (title/location/url come
from the API); skills are enriched for the new candidates in ONE batched
Gemini call per target run. All discovered postings flow through the same
verify -> match -> dedup-merge persistence as manual ingestion.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from typing import List, Optional, Tuple

import httpx
import trafilatura
from sqlmodel import Session

from src.config import settings
from src.database import (
    find_existing_job,
    list_watch_targets,
    log_scheduler_event,
    save_or_update_job_posting,
    save_raw_ingestion,
    today_local,
    touch_watch_target,
)
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    VerificationStatus,
)
from src.models.system import SchedulerEventType, WatchTarget, WatchTargetType
from src.services.extractor import (
    ExtractedJobData,
    extract_jobs_list_from_text,
    extract_multiple_jobs_from_text,
)
from src.services.profile import evaluate_job_match
from src.services.scraper import scrape_url
from src.services.verifier import (
    detect_scam_signals,
    summarize_verification_source,
    verify_opportunity,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
DESCRIPTION_SNIPPET_LIMIT = 4000

INTAKE_TITLE_KEYWORDS = (
    "intern", "internship", "trainee", "co-op", "coop", "traineeship",
    "student", "graduate", "campus", "apprentice",
)
EXCLUDE_TITLE_KEYWORDS = (
    "senior", "staff", "principal", "lead", "manager", "director",
    "vp ", "president", "phd", "ph.d", "postdoc", "experienced",
)

GREENHOUSE_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
GREENHOUSE_JOB_API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
LEVER_API = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_API = "https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"

_HEADERS = {"User-Agent": "AI-Internship-Assistant/1.1"}


@dataclass
class DiscoveredListing:
    """Normalized single posting fetched from a board or careers page."""
    title: str
    application_url: Optional[str] = None
    locations: List[str] = field(default_factory=list)
    description_text: Optional[str] = None
    deadline_text: Optional[str] = None
    board_ref: Optional[str] = None


@dataclass
class DiscoveredJobOutcome:
    """Result row for an ingested (or deduplicated) discovered posting."""
    job: JobPosting
    is_new: bool
    match_score: float
    description_text: Optional[str] = None


# ==============================================================================
# HTTP + Parsing Helpers
# ==============================================================================

def _http_get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Injectable wrapper (tests monkeypatch this) for JSON GET requests."""
    with httpx.Client(timeout=timeout, headers=_HEADERS, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()


def is_internship_title(title: Optional[str]) -> bool:
    """True only for student/intern-level titles; rejects senior/experienced titles."""
    if not title:
        return False
    t = title.lower()
    if any(word in t for word in EXCLUDE_TITLE_KEYWORDS):
        return False
    return any(k in t for k in INTAKE_TITLE_KEYWORDS)


# "2026-10-21" | "2026-10-21T05:00:00+05:30" | "closes on 2026-11-02" -> "YYYY-MM-DD"
_DEADLINE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_deadline_any_format(value) -> Optional[str]:
    """Best-effort conversion of a board's deadline field to YYYY-MM-DD, else None."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        pass
    match = _DEADLINE_DATE_RE.search(value)
    return match.group(0) if match else None


def _to_date(deadline_str: Optional[str]) -> Optional[Date]:
    if not deadline_str:
        return None
    try:
        return datetime.strptime(deadline_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# ==============================================================================
# Board Fetchers (each returns normalized DiscoveredListings)
# ==============================================================================

def fetch_greenhouse_board(token: str, timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredListing]:
    """List postings from a public Greenhouse board (descriptions fetched separately)."""
    data = _http_get_json(GREENHOUSE_API.format(token=token), timeout)
    listings = []
    for j in data.get("jobs", []):
        loc = j.get("location")
        loc = loc.get("name") if isinstance(loc, dict) else loc
        listings.append(DiscoveredListing(
            title=j.get("title", ""),
            application_url=j.get("absolute_url"),
            locations=[loc] if loc else [],
            board_ref=str(j["id"]) if j.get("id") else None,
        ))
    return listings


def fetch_greenhouse_job_content(token: str, job_ref: str, timeout: float = DEFAULT_TIMEOUT) -> Optional[str]:
    """Fetch the full HTML description of one Greenhouse job, reduced to text."""
    try:
        data = _http_get_json(GREENHOUSE_JOB_API.format(token=token, job_id=job_ref), timeout)
        html = data.get("content", "")
        if not html:
            return None
        text = trafilatura.extract(html, output_format="txt") or html
        return text.strip()[:DESCRIPTION_SNIPPET_LIMIT] or None
    except Exception as exc:
        logger.debug(f"Greenhouse content fetch failed for {token}/{job_ref}: {exc}")
        return None


def fetch_lever_board(slug: str, timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredListing]:
    """List postings from a public Lever board (mode=json includes descriptions)."""
    data = _http_get_json(LEVER_API.format(slug=slug), timeout)
    listings = []
    for j in data:
        loc = (j.get("categories") or {}).get("location")
        listings.append(DiscoveredListing(
            title=j.get("text", ""),
            application_url=j.get("applyUrl") or j.get("hostedUrl"),
            locations=[loc] if loc else [],
            description_text=(j.get("descriptionPlain") or "")[:DESCRIPTION_SNIPPET_LIMIT] or None,
            board_ref=str(j["id"]) if j.get("id") else None,
        ))
    return listings


def fetch_ashby_board(slug: str, timeout: float = DEFAULT_TIMEOUT) -> List[DiscoveredListing]:
    """List postings from a public Ashby board."""
    data = _http_get_json(ASHBY_API.format(slug=slug), timeout)
    listings = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = []
        loc = j.get("location") or (j.get("address") or {}).get("postalAddress")
        if loc:
            locs.append(loc)
        for extra in (j.get("secondaryLocations") or []):
            if isinstance(extra, str):
                locs.append(extra)
            elif isinstance(extra, dict) and extra.get("location"):
                locs.append(extra["location"])
        listings.append(DiscoveredListing(
            title=j.get("title", ""),
            application_url=j.get("applyUrl") or j.get("jobUrl"),
            locations=[l for l in locs if isinstance(l, str)][:6],
            description_text=(j.get("descriptionPlain") or "")[:DESCRIPTION_SNIPPET_LIMIT] or None,
            board_ref=str(j["id"]) if j.get("id") else None,
        ))
    return listings


def fetch_custom_url(url: str, timeout: float = 20.0) -> List[DiscoveredListing]:
    """Scrape a careers page; the whole page text becomes one LIST-extract input."""
    result = scrape_url(url, timeout=timeout)
    if not result.success or not result.text or len(result.text.strip()) < 30:
        raise RuntimeError(f"Scrape failed for {url}: {result.error_message or 'empty content'}")
    return [DiscoveredListing(
        title="",
        application_url=result.final_url,
        description_text=result.text[:15000],
    )]


def fetch_target_listings(target: WatchTarget) -> List[DiscoveredListing]:
    """Dispatch a watch target to its board-specific fetcher."""
    if target.target_type == WatchTargetType.GREENHOUSE_BOARD:
        return fetch_greenhouse_board(target.target_key)
    if target.target_type == WatchTargetType.LEVER_BOARD:
        return fetch_lever_board(target.target_key)
    if target.target_type == WatchTargetType.ASHBY_BOARD:
        return fetch_ashby_board(target.target_key)
    if target.target_type == WatchTargetType.CUSTOM_URL:
        return fetch_custom_url(target.target_key)
    raise ValueError(f"Unsupported watch target type: {target.target_type}")


# ==============================================================================
# Persist + Enrich Logic
# ==============================================================================

def _build_job_from_listing(
    target: WatchTarget,
    title: str,
    url: Optional[str],
    locations: List[str],
    description_text: Optional[str],
    deadline_text: Optional[str],
    extracted: Optional[ExtractedJobData],
    raw_ingestion_id: Optional[int],
) -> Tuple[JobPosting, float]:
    """Assemble verification + match scoring for one discovered posting."""
    scam_text = " \n".join(filter(None, [description_text, title, url]))
    red_flags = detect_scam_signals(scam_text[:4000], url=url)

    if red_flags:
        verification_status = VerificationStatus.SUSPICIOUS
        is_official = False
        verification_source = "Red flags: " + "; ".join(red_flags)
    elif target.target_type == WatchTargetType.CUSTOM_URL:
        verif = verify_opportunity(
            company_name=target.company_name,
            role_title=title,
            url=url,
            raw_text=description_text or "",
        )
        verification_status = verif.verification_status
        is_official = verif.is_official
        verification_source = summarize_verification_source(verif, fallback_url=url)
        if verif.official_portal_url:
            url = verif.official_portal_url
    else:
        verification_status = VerificationStatus.VERIFIED_OFFICIAL
        is_official = True
        verification_source = f"{target.target_type.value} board '{target.target_key}'"

    required = list(extracted.required_skills) if extracted else []
    preferred = list(extracted.preferred_skills) if extracted else []
    grad_years = list(extracted.allowed_grad_years) if extracted else []
    deadline = _to_date(parse_deadline_any_format(deadline_text)) if deadline_text else None
    if deadline is None and extracted:
        deadline = _to_date(extracted.application_deadline)

    match_eval = evaluate_job_match(
        role_title=title,
        locations=locations,
        allowed_grad_years=grad_years,
        required_skills=required,
    )

    notes_parts = []
    if red_flags:
        notes_parts.append(f"⚠️ RED FLAGS: {'; '.join(red_flags)}")
    notes_parts.append(f"🛰️ Discovered via watch '{target.company_name}' ({target.target_type.value})")
    notes_parts.append(
        f"Match Score: {int(match_eval.overall_match_score * 100)}% | "
        f"Location: {match_eval.location_fit} | "
        f"Matching Skills: {', '.join(match_eval.matching_skills) or 'None'} | "
        f"Skill Gaps: {', '.join(match_eval.missing_skills) or 'None'}"
    )

    job = JobPosting(
        raw_ingestion_id=raw_ingestion_id,
        company_name=target.company_name,
        role_title=title,
        locations=locations,
        application_deadline=deadline,
        application_url=url,
        allowed_grad_years=grad_years,
        eligibility_status=match_eval.batch_status,
        eligibility_reason=match_eval.batch_reason,
        required_skills=required,
        preferred_skills=preferred,
        verification_status=verification_status,
        verification_source=verification_source,
        is_official_portal=is_official,
        application_status=ApplicationStatus.NEW,
        notes=" | ".join(notes_parts),
    )
    return job, match_eval.overall_match_score


def _check_custom_url_target(
    session: Session,
    target: WatchTarget,
    page: DiscoveredListing,
    max_per_run: int = settings.DISCOVERY_MAX_PER_RUN,
    llm_client=None,
) -> List[DiscoveredJobOutcome]:
    """Extract multiple postings from a scraped careers page and persist new ones."""
    outcomes: List[DiscoveredJobOutcome] = []
    raw_record = save_raw_ingestion(
        session=session,
        source_type=IngestionSource.DISCOVERY,
        raw_content=f"watch_target:{target.id}:{target.target_type.value}:{target.target_key}",
    )

    extracted_jobs = extract_jobs_list_from_text(
        page.description_text or "", client=llm_client, max_jobs=max_per_run
    )

    for data in extracted_jobs:
        title = data.role_title
        if not title or not is_internship_title(title):
            continue
        if find_existing_job(session, target.company_name, title) is not None:
            continue

        job, score = _build_job_from_listing(
            target=target,
            title=title,
            url=data.application_url or page.application_url,
            locations=list(data.locations),
            description_text=None,
            deadline_text=data.application_deadline,
            extracted=data,
            raw_ingestion_id=raw_record.id,
        )
        saved, is_new = save_or_update_job_posting(session, job)
        outcomes.append(DiscoveredJobOutcome(
            job=saved, is_new=is_new, match_score=score, description_text=None,
        ))

    raw_record.processed = True
    session.add(raw_record)
    session.commit()
    return outcomes


def _check_board_target(
    session: Session,
    target: WatchTarget,
    listings: List[DiscoveredListing],
    max_per_run: int = settings.DISCOVERY_MAX_PER_RUN,
    llm_client=None,
) -> List[DiscoveredJobOutcome]:
    """Filter board listings to untracked intern titles, batch-enrich, persist."""
    outcomes: List[DiscoveredJobOutcome] = []
    candidates = [l for l in listings if is_internship_title(l.title)]
    candidates = [
        l for l in candidates
        if find_existing_job(session, target.company_name, l.title) is None
    ][:max_per_run]

    if not candidates:
        return outcomes

    raw_record = save_raw_ingestion(
        session=session,
        source_type=IngestionSource.DISCOVERY,
        raw_content=f"watch_target:{target.id}:{target.target_type.value}:{target.target_key}",
    )

    # Resolve description text (Lever/Ashby have it inline; Greenhouse needs per-job fetch)
    desc_by_id = {}
    for l in candidates:
        text = l.description_text
        if not text and target.target_type == WatchTargetType.GREENHOUSE_BOARD and l.board_ref:
            text = fetch_greenhouse_job_content(target.target_key, l.board_ref)
            l.description_text = text
        if text:
            desc_by_id[id(l)] = text

    extraction_by_id = {}
    if desc_by_id:
        enrich_listings = [l for l in candidates if id(l) in desc_by_id]
        blocks = [
            f"{l.title}\n{desc_by_id[id(l)]}"
            for l in enrich_listings
        ]
        results = extract_multiple_jobs_from_text(blocks, client=llm_client)
        for l, data in zip(enrich_listings, results):
            if data is not None:
                extraction_by_id[id(l)] = data

    for l in candidates:
        extracted = extraction_by_id.get(id(l))
        job, score = _build_job_from_listing(
            target=target,
            title=l.title,
            url=l.application_url,
            locations=list(l.locations),
            description_text=desc_by_id.get(id(l)),
            deadline_text=l.deadline_text,
            extracted=extracted,
            raw_ingestion_id=raw_record.id,
        )
        saved, is_new = save_or_update_job_posting(session, job)
        outcomes.append(DiscoveredJobOutcome(
            job=saved, is_new=is_new, match_score=score, description_text=desc_by_id.get(id(l)),
        ))

    raw_record.processed = True
    session.add(raw_record)
    session.commit()
    return outcomes


def run_discovery(
    session: Session,
    ref_date: Optional[Date] = None,
    llm_client=None,
    max_per_run: int = settings.DISCOVERY_MAX_PER_RUN,
) -> List[DiscoveredJobOutcome]:
    """Poll every active watch target once, ingesting genuinely new postings.

    Returns only NEW outcomes across all targets; dedup merges are excluded.
    """
    new_outcomes: List[DiscoveredJobOutcome] = []
    today = ref_date or today_local()

    targets = list_watch_targets(session, active_only=True)
    if not targets:
        return new_outcomes

    for target in targets:
        try:
            listings = fetch_target_listings(target)
            if target.target_type == WatchTargetType.CUSTOM_URL:
                page = listings[0]
                outcomes = _check_custom_url_target(
                    session, target, page, llm_client=llm_client, max_per_run=max_per_run
                )
            else:
                outcomes = _check_board_target(
                    session, target, listings, llm_client=llm_client, max_per_run=max_per_run
                )
            touch_watch_target(session, target.id)
            new_outcomes.extend(o for o in outcomes if o.is_new)
        except Exception as exc:
            logger.warning(f"Discovery failed for target #{target.id} '{target.company_name}': {exc}")
            touch_watch_target(session, target.id, error=str(exc)[:400])

    log_scheduler_event(
        session,
        SchedulerEventType.DISCOVERY_RUN,
        "all_targets",
        today,
        detail=f"{len(new_outcomes)} new postings from {len(targets)} targets",
    )
    return new_outcomes


def is_alertable(outcome: DiscoveredJobOutcome, min_score: float = settings.DISCOVERY_ALERT_MIN_SCORE) -> bool:
    """A discovered job deserves a Telegram push if it's fresh, not flagged, and relevant."""
    job = outcome.job
    if job.verification_status == VerificationStatus.SUSPICIOUS:
        return False
    if job.eligibility_status == EligibilityStatus.INELIGIBLE:
        return False
    if outcome.match_score < min_score:
        return False
    return True
