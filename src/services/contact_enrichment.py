"""Prospeo Contact Enrichment Adapter (P1 Outreach).

Turns a LinkedIn profile URL into a verified work email via the Prospeo
"Enrich Person" REST API, feeding candidates into the approved cold-outreach
flow. Designed around the free plan's economics so the user never accidentally
burns their monthly quota:

- Free plan: 100 credits/month, 1 credit per revealed verified email.
  ``only_verified_email=true`` means profiles without a verified email come
  back as NO_MATCH and are never charged.
- Enrich rate limits (free tier): 1 req/sec, 20/min, 50/day — we additionally
  self-throttle (min 1.1s between calls) and let the bot layer enforce a daily
  attempt budget via the scheduler log.
- All API failures arrive as HTTP 400/429; always branch on the JSON
  ``error``/``error_code`` before trusting the status code.
- Re-enriching the same record within 90 days is free (``free_enrichment``).
- The API key is user-owned (.env) and never logged or echoed. When the key is
  rejected or credits run out, the raised error message tells the user exactly
  how to swap the key manually (one key per .env; no automated rotation —
  Prospeo's free plan flags multi-accounting).

Endpoints (verified against https://prospeo.io/api-docs, June 2026):
- POST {base}/enrich-person
- GET  {base}/account-information  (free usage/credits snapshot)
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import httpx

from src.config import settings
from src.models.contact import EmailStatus

logger = logging.getLogger(__name__)

PROSPEO_API_BASE = "https://api.prospeo.io"
ENDPOINT_ENRICH_PERSON = f"{PROSPEO_API_BASE}/enrich-person"
ENDPOINT_ACCOUNT_INFO = f"{PROSPEO_API_BASE}/account-information"

# Error codes returned in the JSON body (HTTP 400) or as a bare 429.
CODE_NO_MATCH = "NO_MATCH"
CODE_INVALID_DATAPOINTS = "INVALID_DATAPOINTS"
CODE_INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"
CODE_INVALID_API_KEY = "INVALID_API_KEY"
CODE_INVALID_REQUEST = "INVALID_REQUEST"
CODE_INTERNAL_ERROR = "INTERNAL_ERROR"
CODE_RATE_LIMITED = "RATE_LIMITED"  # synthetic: bare HTTP 429
CODE_BAD_RESPONSE = "BAD_RESPONSE"  # synthetic: non-JSON / unexpected shape
CODE_NETWORK = "NETWORK"  # synthetic: transport failure
CODE_UNKNOWN = "UNKNOWN"

PROSPEO_PLACEHOLDER_MARKERS = ("your", "xxxx", "1234", "placeholder")
PROSPEO_MIN_KEY_LEN = 16

_MESSAGE_BY_CODE = {
    CODE_NO_MATCH: (
        "No verified email found for this profile (0 credits charged). "
        "Contact is saved without an email — attach one manually later via /addcontact."
    ),
    CODE_INVALID_DATAPOINTS: "Prospeo couldn't identify the person from the datapoints provided.",
    CODE_INSUFFICIENT_CREDITS: (
        "Prospeo monthly credits exhausted. Check /prospeo for the renewal date, or swap in a "
        "fresh PROSPEO_API_KEY in .env and restart the bot."
    ),
    CODE_INVALID_API_KEY: (
        "Prospeo rejected the API key. Paste the current key from your Prospeo dashboard into "
        "PROSPEO_API_KEY in .env and restart the bot."
    ),
    CODE_INVALID_REQUEST: "Prospeo rejected the request as invalid.",
    CODE_INTERNAL_ERROR: "Prospeo had an internal error — try again shortly.",
}


class ProspeoError(RuntimeError):
    """API-level failure with a machine-readable ``code`` for bot UX branching."""

    def __init__(
        self,
        message: str,
        code: str = CODE_UNKNOWN,
        retry_after_seconds: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after_seconds = retry_after_seconds


@dataclass
class EnrichedContact:
    """Result of a successful Prospeo enrichment lookup."""
    full_name: str = ""
    email: Optional[str] = None
    role_title: Optional[str] = None
    company_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    email_status: EmailStatus = EmailStatus.PROSPEO_VERIFIED
    person_id: Optional[str] = None
    free_enrichment: bool = False  # True when re-enriched within the 90-day window (0 credits)
    credits_used: int = 1
    daily_requests_left: Optional[int] = None
    raw_payload: Dict = field(default_factory=dict)


@dataclass
class ProspeoAccount:
    """Snapshot from the free /account-information endpoint."""
    current_plan: str = "UNKNOWN"
    remaining_credits: Optional[int] = None
    used_credits: Optional[int] = None
    next_quota_renewal_days: Optional[int] = None
    next_quota_renewal_date: Optional[str] = None


def validate_prospero_api_key(api_key: Optional[str]) -> str:
    """Enforce a non-placeholder Prospeo key with an actionable error message."""
    if not api_key or not api_key.strip():
        raise ValueError(
            "PROSPEO_API_KEY is missing in .env. Create a free account at https://app.prospeo.io "
            "and copy your API key (the free plan includes 100 credits/month + API access)."
        )
    key = api_key.strip()
    lowered = key.lower()
    if lowered in PROSPEO_PLACEHOLDER_MARKERS or any(m in lowered for m in PROSPEO_PLACEHOLDER_MARKERS):
        raise ValueError(
            f"PROSPEO_API_KEY looks like a placeholder ('{key[:4]}...'). Paste the real key from your Prospeo dashboard."
        )
    if len(key) < PROSPEO_MIN_KEY_LEN:
        raise ValueError(
            f"PROSPEO_API_KEY looks truncated ({len(key)} chars; real keys are longer). Re-copy the full value."
        )
    return key


def enrichment_available(api_key: Optional[str] = None) -> bool:
    """Whether a usable key is configured. Non-raising."""
    try:
        validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
        return True
    except ValueError:
        return False


def enrichment_enabled() -> bool:
    """Master kill-switch state (PROSPEO_ENRICH_ENABLED). Non-raising."""
    return bool(settings.PROSPEO_ENRICH_ENABLED)


# ==============================================================================
# Self-throttling (free tier allows 1 request/second)
# ==============================================================================

_last_call_at: Optional[float] = None


def _reset_throttle_state() -> None:
    """Test hook: clear the module-level throttle clock."""
    global _last_call_at
    _last_call_at = None


def _throttle(
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    min_interval: Optional[float] = None,
) -> float:
    """Guarantee a minimum gap between successive Prospeo calls; returns wait duration."""
    global _last_call_at
    gap = settings.PROSPEO_MIN_INTERVAL_SECONDS if min_interval is None else min_interval
    if gap <= 0:
        _last_call_at = clock()
        return 0.0
    now = clock()
    waited = 0.0
    if _last_call_at is not None:
        remaining = gap - (now - _last_call_at)
        if remaining > 0:
            sleeper(remaining)
            waited = remaining
            now = clock()
    _last_call_at = now
    return waited


# ==============================================================================
# Request building & response parsing
# ==============================================================================

def _build_enrich_payload(
    linkedin_url: str,
    full_name: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Dict:
    """Enrich-Person request body: verified-email-only, never a paid mobile reveal.

    Extra datapoints (name/company hints from /addcontact) raise match accuracy
    at no cost. linkedin_url alone already satisfies the matching minimums.
    """
    data: Dict = {"linkedin_url": linkedin_url}
    if full_name:
        data["full_name"] = full_name.strip()
    if company_name:
        data["company_name"] = company_name.strip()
    return {"only_verified_email": True, "enrich_mobile": False, "data": data}


def _header_int(resp: httpx.Response, name: str) -> Optional[int]:
    raw = resp.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rate_limit_message(resp: httpx.Response) -> str:
    daily_left = _header_int(resp, "x-daily-request-left")
    minute_reset = _header_int(resp, "x-minute-reset-seconds")
    daily_reset = _header_int(resp, "x-daily-reset-seconds")
    wait = minute_reset if minute_reset is not None else (daily_reset if daily_reset is not None else 60)
    parts = [f"Prospeo rate limit hit — retry in ~{wait}s"]
    if daily_left is not None:
        parts.append(f"{daily_left} enrich requests left today")
    elif daily_reset is not None:
        parts.append(f"daily window resets in ~{daily_reset}s")
    return "; ".join(parts) + "."


def _raise_for_error_code(error_code: str) -> None:
    message = _MESSAGE_BY_CODE.get(error_code, f"Prospeo enrichment failed ({error_code}).")
    raise ProspeoError(message, code=error_code)


def _parse_enrich_response(resp: httpx.Response, fallback_url: str) -> EnrichedContact:
    """Branch on Prospeo's contract: 429 first, then JSON body's error flag."""
    if resp.status_code == 429:
        raise ProspeoError(_rate_limit_message(resp), code=CODE_RATE_LIMITED)

    try:
        body = resp.json()
    except ValueError as exc:
        raise ProspeoError(
            f"Prospeo returned a non-JSON body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE
        ) from exc
    if not isinstance(body, dict):
        raise ProspeoError(f"Prospeo returned an unexpected body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE)

    if body.get("error") or body.get("error_code"):
        _raise_for_error_code(str(body.get("error_code") or CODE_UNKNOWN))
    if resp.status_code >= 400:
        raise ProspeoError(f"Prospeo request failed with HTTP {resp.status_code}.", code=CODE_BAD_RESPONSE)

    person = body.get("person") or {}
    company = body.get("company") or {}

    email_obj = person.get("email") or {}
    if not isinstance(email_obj, dict):
        email_obj = {}
    email = (email_obj.get("email") or "").strip() if email_obj.get("revealed") else ""
    status = str(email_obj.get("status") or "").upper()
    if not email or status != "VERIFIED":
        # Defensive: with only_verified_email=true this shouldn't happen — if it
        # ever does, refuse to store an unverified/unrevealed address.
        raise ProspeoError(_MESSAGE_BY_CODE[CODE_NO_MATCH], code=CODE_NO_MATCH)

    full_name = (person.get("full_name") or "").strip()
    if not full_name:
        first = (person.get("first_name") or "").strip()
        last = (person.get("last_name") or "").strip()
        full_name = f"{first} {last}".strip()

    free_enrichment = bool(body.get("free_enrichment"))
    return EnrichedContact(
        full_name=full_name,
        email=email,
        role_title=person.get("current_job_title"),
        company_name=company.get("name") if isinstance(company, dict) else None,
        linkedin_url=person.get("linkedin_url") or fallback_url,
        email_status=EmailStatus.PROSPEO_VERIFIED,
        person_id=person.get("person_id"),
        free_enrichment=free_enrichment,
        credits_used=0 if free_enrichment else 1,
        daily_requests_left=_header_int(resp, "x-daily-request-left"),
        raw_payload=body,
    )


# ==============================================================================
# Public API
# ==============================================================================

def enrich_linkedin_profile(
    linkedin_url: str,
    api_key: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
    timeout: float = 10.0,
    full_name: Optional[str] = None,
    company_name: Optional[str] = None,
) -> EnrichedContact:
    """Resolve a verified work email for a single LinkedIn profile URL (1 credit)."""
    key = validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
    url = linkedin_url.strip()
    if "linkedin.com" not in url.lower():
        raise ValueError("Provide a full LinkedIn profile URL (e.g. https://www.linkedin.com/in/handle/).")

    payload = _build_enrich_payload(url, full_name=full_name, company_name=company_name)
    headers = {"X-KEY": key, "Content-Type": "application/json"}
    _throttle()

    try:
        if http_client is not None:
            resp = http_client.post(ENDPOINT_ENRICH_PERSON, json=payload, headers=headers, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as fresh:
                resp = fresh.post(ENDPOINT_ENRICH_PERSON, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ProspeoError(f"Prospeo is unreachable right now ({exc.__class__.__name__}).", code=CODE_NETWORK) from exc

    result = _parse_enrich_response(resp, url)
    if result.credits_used:
        logger.info("Prospeo enrichment charged 1 credit (daily_request_left=%s)", result.daily_requests_left)
    else:
        logger.info("Prospeo enrichment was free (90-day repeat window)")
    return result


def get_prospeo_account(
    api_key: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
    timeout: float = 10.0,
) -> ProspeoAccount:
    """Free usage snapshot: plan, credits remaining/used, next renewal."""
    key = validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
    headers = {"X-KEY": key}
    try:
        if http_client is not None:
            resp = http_client.get(ENDPOINT_ACCOUNT_INFO, headers=headers, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as fresh:
                resp = fresh.get(ENDPOINT_ACCOUNT_INFO, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ProspeoError(f"Prospeo is unreachable right now ({exc.__class__.__name__}).", code=CODE_NETWORK) from exc

    if resp.status_code == 429:
        raise ProspeoError(_rate_limit_message(resp), code=CODE_RATE_LIMITED)
    try:
        body = resp.json()
    except ValueError as exc:
        raise ProspeoError(
            f"Prospeo returned a non-JSON body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE
        ) from exc
    if not isinstance(body, dict):
        raise ProspeoError(f"Prospeo returned an unexpected body (HTTP {resp.status_code}).", code=CODE_BAD_RESPONSE)
    if body.get("error") or body.get("error_code"):
        _raise_for_error_code(str(body.get("error_code") or CODE_UNKNOWN))

    snapshot = body.get("response") or {}
    return ProspeoAccount(
        current_plan=str(snapshot.get("current_plan") or "UNKNOWN"),
        remaining_credits=_coerce_int(snapshot.get("remaining_credits")),
        used_credits=_coerce_int(snapshot.get("used_credits")),
        next_quota_renewal_days=_coerce_int(snapshot.get("next_quota_renewal_days")),
        next_quota_renewal_date=snapshot.get("next_quota_renewal_date"),
    )
