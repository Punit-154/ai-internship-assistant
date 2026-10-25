"""Prospeo Contact Enrichment Adapter (P1 Outreach).

Turns a LinkedIn profile URL into a verified work email via the Prospeo
contact-data API, storing candidates for the approved cold-outreach flow.

Design notes:
- Enrichment is strictly optional: with no PROSPEO_API_KEY configured the
  system degrades gracefully to manual email entry (never crashes).
- Prospeo returns HTTP 400 for almost every failure (including auth problems);
  consumers must branch on the JSON `error_code` in the body, not the status.
- The API key is user-owned (.env) and never logged or echoed.
- The enrich endpoint path is provisional until live-tested with a real key —
  Prospeo removed the legacy email-finder endpoints in March 2026 in favour of
  an "Enrich Person" source; adjust the path to their current docs when a key
  goes live.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx

from src.config import settings
from src.models.contact import EmailStatus

logger = logging.getLogger(__name__)

PROSPEO_API_BASE = "https://api.prospeo.io"
# Provisional paths (verify against https://prospeo.io/api-docs when a key goes live)
ENDPOINT_LINKEDIN_ENRICH = f"{PROSPEO_API_BASE}/api/linkedin-enrichment"
ENDPOINT_EMAIL_ENRICH = f"{PROSPEO_API_BASE}/api/enrich"

PROSPEO_PLACEHOLDER_MARKERS = ("1234", "your", "xxxx", "placeholder", "test", "sample")
PROSPEO_MIN_KEY_LEN = 16


class ProspeoError(RuntimeError):
    """Raised on API-level failures (bad key, not found, rate limited, malformed body)."""


@dataclass
class EnrichedContact:
    """Result of a successful Prospeo enrichment lookup."""
    full_name: str = ""
    email: Optional[str] = None
    role_title: Optional[str] = None
    company_name: Optional[str] = None
    linkedin_url: Optional[str] = None
    email_status: EmailStatus = EmailStatus.PROSPEO_VERIFIED
    raw_payload: Dict = field(default_factory=dict)


def validate_prospero_api_key(api_key: Optional[str]) -> str:
    """Enforce a non-placeholder Prospeo key with an actionable error message."""
    if not api_key or not api_key.strip():
        raise ValueError(
            "PROSPEO_API_KEY is missing in .env. Create one at https://prospeo.io (paid "
            "plans include API access; the free plan is extension-only)."
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
    """Whether enrichment can run right now (key configured). Non-raising."""
    try:
        validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
        return True
    except ValueError:
        return False


def _extract_profile_payload(body: Dict) -> Dict:
    """Unwrap common response envelope shapes into the profile payload."""
    for wrapper in ("data", "result", "profile", "person"):
        nested = body.get(wrapper)
        if isinstance(nested, dict) and (nested.get("email") or nested.get("full_name")):
            return nested
    return body


def enrich_linkedin_profile(
    linkedin_url: str,
    api_key: Optional[str] = None,
    http_client: Optional[httpx.Client] = None,
    timeout: float = 10.0,
) -> EnrichedContact:
    """Resolve a verified work email for a single LinkedIn profile URL."""
    key = validate_prospero_api_key(api_key or settings.PROSPEO_API_KEY)
    url = linkedin_url.strip()
    if "linkedin.com" not in url.lower():
        raise ValueError("Provide a full LinkedIn profile URL (e.g. https://www.linkedin.com/in/handle/).")

    headers = {"X-KEY": key, "Content-Type": "application/json"}
    payload = {"linkedin_url": url}

    try:
        if http_client is not None:
            resp = http_client.post(ENDPOINT_LINKEDIN_ENRICH, json=payload, headers=headers, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as fresh:
                resp = fresh.post(ENDPOINT_LINKEDIN_ENRICH, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ProspeoError(f"Prospeo request failed: {exc}") from exc

    try:
        body = resp.json()
    except ValueError as exc:
        raise ProspeoError(f"Prospeo returned a non-JSON body (HTTP {resp.status_code}).") from exc

    if resp.status_code >= 400 or isinstance(body, dict) and body.get("error_code"):
        error_code = (body or {}).get("error_code") or "HTTP_ERROR"
        if "api_key" in str(error_code).lower() or resp.status_code == 401:
            raise ProspeoError("Prospeo rejected the API key — re-copy it from the Prospeo dashboard.")
        raise ProspeoError(f"Prospeo enrichment failed ({error_code}).")
    if resp.status_code == 204 or not body:
        raise ProspeoError("No match found on Prospeo for this profile.")

    profile = _extract_profile_payload(body)
    email = profile.get("email") or profile.get("email_address")
    if not email:
        raise ProspeoError("Prospeo matched the profile but returned no verified email (credits not charged upstream).")

    return EnrichedContact(
        full_name=(profile.get("full_name") or profile.get("name") or "").strip(),
        email=(email or "").strip(),
        role_title=profile.get("job_title") or profile.get("title"),
        company_name=profile.get("company") or profile.get("company_name"),
        linkedin_url=profile.get("linkedin_url") or url,
        email_status=EmailStatus.PROSPEO_VERIFIED,
        raw_payload=profile,
    )
