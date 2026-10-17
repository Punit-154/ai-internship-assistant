"""Opportunity Verification and Authenticity Service (Milestone 5).

Verifies the legitimacy of internship opportunities, detects scams/red flags
(registration fees, suspicious contact channels, non-corporate emails), and
leverages DuckDuckGo Search Grounding to discover authentic official career portals.
"""

import logging
import re
from typing import Optional, List, Tuple
from urllib.parse import urlparse
from pydantic import BaseModel, Field
from ddgs import DDGS
from src.models.job import VerificationStatus

logger = logging.getLogger(__name__)

# Known verified Applicant Tracking Systems and primary careers portals
TRUSTED_ATS_DOMAINS = {
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "bamboohr.com",
    "icims.com",
    "jobvite.com",
    "taleo.net",
    "oraclecloud.com",
    "amazon.jobs",
    "careers.google.com",
    "careers.microsoft.com",
    "metacareers.com",
    "apple.com",
    "uber.com",
    "netflix.com",
    "workable.com",
}

# Red flag terms indicative of job scams and fraudulent postings
SCAM_SIGNALS = [
    (r"\b(registration\s+fee|security\s+deposit|training\s+fee|laptop\s+deposit|processing\s+charges?)\b", "Demands upfront payment or registration fee"),
    (r"\b(pay\s+(?:rs|inr|\$|\u20B9)\s*\d+|deposit\s+money|send\s+money)\b", "Demands money transfer or application fee"),
    (r"\b(crypto(?:currency)?\s+task|like\s+(?:and\s+subscribe\s+)?youtube\s+videos?|telegram\s+task)\b", "Task-based or cryptocurrency review scam"),
    (r"\b(contact\s+(?:us\s+)?on\s+whatsapp\s+only|dm\s+on\s+telegram\s+to\s+apply)\b", "Directs exclusively to personal WhatsApp or Telegram for hiring"),
]

FREE_EMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com"}


class VerificationResult(BaseModel):
    """Structured report on job authenticity and legitimacy."""
    is_official: bool = Field(description="True if confirmed official company portal or verified ATS")
    verification_status: VerificationStatus = Field(description="VERIFIED_OFFICIAL, UNVERIFIED_THIRD_PARTY, or SUSPICIOUS")
    official_portal_url: Optional[str] = Field(default=None, description="Direct authentic careers URL if found via search")
    red_flags: List[str] = Field(default_factory=list, description="Detected red flags or scam warning signs")
    notes: str = Field(default="", description="Human-readable verification explanation")


def detect_scam_signals(text: str, url: Optional[str] = None) -> List[str]:
    """Scan job description and application details for scam indicators."""
    flags: List[str] = []
    if not text:
        return flags

    lower_text = text.lower()

    for pattern, description in SCAM_SIGNALS:
        if re.search(pattern, lower_text):
            flags.append(description)

    # Check for personal email in corporate postings
    emails = re.findall(r"[\w\.-]+@[\w\.-]+\.\w+", lower_text)
    for email in emails:
        domain = email.split("@")[-1].lower()
        if domain in FREE_EMAIL_DOMAINS:
            flags.append(f"Uses personal email address for corporate hiring ({email})")

    # Check suspicious URL domains
    if url:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if "t.me" in hostname or "telegram.me" in hostname:
            flags.append("Application directs to an informal Telegram channel")

    return list(dict.fromkeys(flags))


def search_official_career_portal(company_name: str, role_title: str) -> Optional[str]:
    """Search DuckDuckGo to locate the authentic official careers portal or direct apply link for a given role."""
    if not company_name or company_name.lower() in {"unknown", "various"}:
        return None

    REPUTABLE_JOB_PORTALS = (
        "linkedin.com/jobs/view",
        "wellfound.com/jobs",
        "unstop.com/internships",
        "internshala.com/internship/detail",
    )

    queries = [
        f'"{company_name}" "{role_title}" careers OR jobs',
        f'{company_name} {role_title} internship apply careers',
    ]

    fallback_reputable_url: Optional[str] = None

    for query in queries:
        logger.info(f"Searching official career portal for: {query}")
        try:
            results = list(DDGS().text(query, max_results=5))
            for item in results:
                href = item.get("href", "")
                if not href.startswith(("http://", "https://")):
                    continue
                parsed = urlparse(href)
                hostname = (parsed.hostname or "").lower()

                # Check if domain matches trusted ATS
                for ats in TRUSTED_ATS_DOMAINS:
                    if hostname.endswith(ats):
                        logger.info(f"Discovered trusted ATS portal: {href}")
                        return href

                # Check if domain matches company name directly
                cleaned_company = "".join(c for c in company_name.lower() if c.isalnum())
                if cleaned_company and cleaned_company in hostname:
                    if any(k in href.lower() for k in ["job", "career", "posting", "role", "position"]):
                        logger.info(f"Discovered official company domain portal: {href}")
                        return href

                # Track reputable fallback platform if no direct portal seen yet
                if not fallback_reputable_url:
                    for rep in REPUTABLE_JOB_PORTALS:
                        if rep in href.lower():
                            fallback_reputable_url = href
        except Exception as exc:
            logger.warning(f"Search grounding query '{query}' failed: {exc}")

    if fallback_reputable_url:
        logger.info(f"Discovered reputable job platform apply link: {fallback_reputable_url}")
        return fallback_reputable_url

    return None


def verify_opportunity(
    company_name: str,
    role_title: str,
    url: Optional[str] = None,
    raw_text: Optional[str] = None,
) -> VerificationResult:
    """Comprehensive verification evaluating domain authenticity, scam signals, and search grounding.
    
    Args:
        company_name: Company name identified in the posting.
        role_title: Job title or role.
        url: Provided application URL (if any).
        raw_text: Full text or context of the job posting.
        
    Returns:
        VerificationResult with status, official URL, flags, and notes.
    """
    red_flags = detect_scam_signals(raw_text or "", url=url)

    # If severe scam signals are detected
    if red_flags:
        logger.warning(f"Potential scam detected for {company_name} - {role_title}: {red_flags}")
        return VerificationResult(
            is_official=False,
            verification_status=VerificationStatus.SUSPICIOUS,
            official_portal_url=url,
            red_flags=red_flags,
            notes=f"CRITICAL WARNING: Potential scam detected. Flags: {'; '.join(red_flags)}",
        )

    # If application URL is provided, evaluate domain
    if url:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()

        # Check trusted ATS
        for ats in TRUSTED_ATS_DOMAINS:
            if hostname.endswith(ats):
                return VerificationResult(
                    is_official=True,
                    verification_status=VerificationStatus.VERIFIED_OFFICIAL,
                    official_portal_url=url,
                    red_flags=[],
                    notes=f"Verified official ATS portal: {ats}",
                )

        # Check official company domain
        cleaned_company = "".join(c for c in company_name.lower() if c.isalnum())
        if cleaned_company and cleaned_company in hostname:
            return VerificationResult(
                is_official=True,
                verification_status=VerificationStatus.VERIFIED_OFFICIAL,
                official_portal_url=url,
                red_flags=[],
                notes=f"Official company careers domain: {hostname}",
            )

        # Flag third-party forms or redirectors
        if "docs.google.com" in hostname or "forms.gle" in hostname:
            grounded_url = search_official_career_portal(company_name, role_title)
            return VerificationResult(
                is_official=bool(grounded_url),
                verification_status=VerificationStatus.VERIFIED_OFFICIAL if grounded_url else VerificationStatus.UNVERIFIED_THIRD_PARTY,
                official_portal_url=grounded_url or url,
                red_flags=["Application hosted on Google Forms rather than an official ATS"],
                notes="Caution: Application link is a Google Form." + (f" Found authentic official portal: {grounded_url}" if grounded_url else ""),
            )

    # If URL is missing, or is an unverified third party (aggregators like LinkedIn, Unstop, Hiredoor, Internshala)
    grounded_url = search_official_career_portal(company_name, role_title)
    if grounded_url:
        return VerificationResult(
            is_official=True,
            verification_status=VerificationStatus.VERIFIED_OFFICIAL,
            official_portal_url=grounded_url,
            red_flags=[],
            notes=f"Verified via search grounding: Authentic official listing discovered at {grounded_url}",
        )

    return VerificationResult(
        is_official=False,
        verification_status=VerificationStatus.UNVERIFIED_THIRD_PARTY,
        official_portal_url=url,
        red_flags=[],
        notes="Third-party aggregator or unverified link. No direct official company portal found via search grounding.",
    )
