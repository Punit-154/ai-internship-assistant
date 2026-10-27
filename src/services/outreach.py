"""Approved Cold-Outreach Engine (P1 Networking).

Pipeline: Contact + Job -> Gemini (or template fallback) draft ->
Telegram Approve/Discard/Edit -> approved sends ONLY via Gmail SMTP ->
sent tracking -> scheduler-driven follow-ups (idempotent via scheduler_log).

Spam-safety design:
- Nothing is ever sent without an explicit Telegram "Approve" click.
- Per-day caps on first-contact and follow-up sends (scheduler-log ledger)..
- Duplicate sends for the same contact on the same day are blocked.
"""

import json
import logging
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr
from typing import List, Optional, Tuple

from sqlmodel import Session, select

from src.config import settings
from src.database import (
    count_scheduler_events,
    get_utc_now,
    has_scheduler_event,
    log_scheduler_event,
    normalize_text_key,
    today_local,
)
from src.models.contact import Contact, ContactSource, EmailStatus, OutreachStatus
from src.models.job import JobPosting
from src.models.system import SchedulerEventType

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
CAP_LEDGER_KEY = "cap_ledger"

# Deterministic send gate: SMTP delivery is IMPOSSIBLE unless a caller passes
# this module-level token. Only approve_and_send() (the Telegram Approve-button
# path) forwards it. Direct calls must pass the token explicitly, which makes
# every send site in the codebase auditable by grep.
SEND_APPROVAL_TOKEN = "user-approved-send"


class OutreachError(RuntimeError):
    """Delivery-side failure (missing credentials, SMTP error, cap reached)."""


@dataclass
class OutreachDraft:
    """A generated (not yet sent) cold email."""
    contact_id: int
    job_id: int
    subject: str
    body: str
    is_followup: bool = False


def _email_credentials() -> Tuple[str, str]:
    user, password = settings.GMAIL_ADDRESS, settings.GMAIL_APP_PASSWORD
    if not user or not password:
        raise OutreachError(
            "Gmail delivery is not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "in .env (use a Gmail App Password, NOT the account password;"
            " create one at https://myaccount.google.com/apppasswords)."
        )
    return user, password


def send_outreach_email(
    to_address: str,
    subject: str,
    body: str,
    smtp_factory=None,
    approval_token: Optional[str] = None,
) -> datetime:
    """Send one approved email through Gmail SMTP; returns the sent timestamp.

    DETERMINISTIC GUARDRAILS (checked before any network I/O):
    1. approval_token must equal SEND_APPROVAL_TOKEN — only the Telegram
       Approve-button path (approve_and_send) passes it, so nothing can ever
       send without an explicit human click.
    2. settings.OUTREACH_SEND_ENABLED is a master kill-switch; when false,
       even approved sends are physically blocked.
    """
    if approval_token != SEND_APPROVAL_TOKEN:
        raise OutreachError(
            "SEND BLOCKED: email delivery requires explicit user approval. "
            "Only the Telegram /draft -> Approve path may send (approval_token=SEND_APPROVAL_TOKEN)."
        )
    if not settings.OUTREACH_SEND_ENABLED:
        raise OutreachError("SEND BLOCKED: OUTREACH_SEND_ENABLED=false (master kill-switch in .env).")

    user, password = _email_credentials()
    safe_to = parseaddr(to_address)[1] or to_address
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((candidate_display_name(), user))
    msg["To"] = safe_to

    factory = smtp_factory or (lambda: smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20.0))
    try:
        with factory() as smtp:
            smtp.login(user, password)
            smtp.sendmail(user, [safe_to], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise OutreachError("Gmail rejected the login - check GMAIL_ADDRESS / GMAIL_APP_PASSWORD (App Password).") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise OutreachError(f"SMTP delivery failed: {exc}") from exc

    logger.info(f"Outreach email sent to {safe_to} (never logged subject/body recipients beyond this).")
    return get_utc_now()


def candidate_display_name() -> str:
    """Candidate name for the From header; degrades to a neutral label in tests."""
    try:
        return get_candidate_profile_safe().full_name
    except Exception:
        return "Candidate"


# ------------------------------------------------------------------------------
# Draft generation (Gemini with deterministic template fallback)
# ------------------------------------------------------------------------------

_TEMPLATE_FIRST = (
    "Subject: {subject}\n\n"
    "Hi {contact_first_name},\n\n"
    "I'm {candidate_name}, a {degree} student at {college} (Class of {grad_year}). "
    "I came across the {role} opening at {company} and it lines up closely with what I have "
    "built - most recently, {project_hook}.\n\n"
    "If your team is considering interns for this role, I'd be glad to share my resume or be "
    "pointed to the right person.\n\n"
    "Best regards,\n{candidate_name}"
)

_TEMPLATE_FOLLOWUP = (
    "Subject: {subject}\n\n"
    "Hi {contact_first_name},\n\n"
    "Following up on my earlier note about the {role} internship at {company}. I remain very "
    "interested and can share scores, references, or more project detail anytime.\n\n"
    "Best regards,\n{candidate_name}"
)


def _first_name(full_name: Optional[str]) -> str:
    name = (full_name or "").strip()
    return name.split()[0] if name else "there"


def _project_hook() -> str:
    try:
        profile = get_candidate_profile_safe()
        projects = profile.verified_projects or []
        if projects:
            top = projects[0]
            return f"{top.title} ({', '.join((top.tech_stack or [])[:3])})"
    except Exception:
        pass
    return "my recent engineering projects"


def get_candidate_profile_safe():
    from src.services.profile import get_candidate_profile
    return get_candidate_profile()


def _template_fallback(contact: Contact, job: JobPosting, followup: bool) -> Tuple[str, str]:
    profile = get_candidate_profile_safe()
    fmt_vars = {
        "contact_first_name": _first_name(contact.full_name),
        "candidate_name": profile.full_name,
        "college": profile.college,
        "degree": profile.degree,
        "grad_year": profile.graduation_year,
        "company": job.company_name,
        "role": job.role_title,
        "project_hook": _project_hook(),
        "subject": f"VIT 2028 student interested in the {job.role_title} internship",
    }
    if followup:
        fmt_vars["subject"] = f"Checking in: {job.role_title} internship at {job.company_name}"
        body = _TEMPLATE_FOLLOWUP.format(**fmt_vars)
    else:
        body = _TEMPLATE_FIRST.format(**fmt_vars)
    return fmt_vars["subject"], body


def craft_outreach_draft(
    contact: Contact,
    job: JobPosting,
    followup: bool = False,
    client=None,
) -> OutreachDraft:
    """Generate a cold email draft with Gemini; falls back to a truthful template offline."""
    subject, body = _try_llm_draft(contact, job, followup, client)
    if subject:
        return OutreachDraft(
            contact_id=contact.id or 0,
            job_id=job.id or 0,
            subject=subject,
            body=body,
            is_followup=followup,
        )
    subject, body = _template_fallback(contact, job, followup)
    return OutreachDraft(contact_id=contact.id or 0, job_id=job.id or 0, subject=subject, body=body, is_followup=followup)


def _try_llm_draft(contact: Contact, job: JobPosting, followup: bool, client=None) -> Tuple[Optional[str], str]:
    from pydantic import BaseModel, Field

    from src.services.extractor import generate_content_with_fallback

    class _Draft(BaseModel):
        subject: str = Field(description="Email subject line, under 80 characters")
        body: str = Field(description="Plain-text email body under 150 words")

    profile = get_candidate_profile_safe()
    draft_type = "short follow-up email" if followup else "first cold email"
    prompt = f"""
You are an expert professional cold-outreach copywriter for a CS undergraduate seeking internships.

TASK: Draft a {draft_type}.
CANDIDATE (STRICT ZERO-FABRICATION): Name: {profile.full_name}; {profile.degree} at {profile.college}
(Class of {profile.graduation_year}); verified skills: {json.dumps(profile.verified_skills)}.
TOP PROJECT: {_project_hook()}
TARGET CONTACT: {json.dumps({'name': contact.full_name, 'role': contact.role_title, 'relationship': contact.relationship, 'company': contact.company_name})}
TARGET JOB: {json.dumps({'role': job.role_title, 'company': job.company_name, 'required_skills': job.required_skills, 'locations': job.locations})}

RULES:
1. Under 150 words, plain text, one specific project hook drawn ONLY from the candidate's real projects.
2. Respectful, crisp, no desperation, no exaggerated claims, no invented names or titles.
3. End with a soft, concrete ask (resume share / quick referral direction), never a demand.
4. Subject line under 80 chars.
"""
    if not client:
        from src.services.extractor import get_genai_client
        try:
            client = get_genai_client()
        except Exception:
            return None, ""

    try:
        from google.genai import types

        response = generate_content_with_fallback(
            client=client,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_Draft,
                temperature=0.4,
            ),
        )
        draft = _Draft.model_validate_json(response.text)
        return draft.subject.strip(), draft.body.strip()
    except Exception as exc:
        logger.warning(f"LLM outreach draft failed; using template fallback: {exc}")
        return None, ""


# ------------------------------------------------------------------------------
# Approved send orchestration + tracking
# ------------------------------------------------------------------------------

def approve_and_send(
    session: Session,
    contact_id: int,
    subject: str,
    body: str,
    followup: bool = False,
    smtp_factory=None,
) -> Contact:
    """Send an approved draft, enforcing caps and same-day duplicate protection."""
    contact = session.get(Contact, contact_id)
    if not contact:
        raise OutreachError(f"Contact #{contact_id} not found.")
    if not contact.email:
        raise OutreachError(f"Contact '{contact.full_name}' has no email address stored.")

    event_type = SchedulerEventType.OUTREACH_FOLLOWUP_SEND if followup else SchedulerEventType.OUTREACH_FIRST_SEND
    daily_cap = settings.OUTREACH_MAX_FOLLOWUPS_PER_DAY if followup else settings.OUTREACH_MAX_FIRST_SENDS_PER_DAY
    today = today_local()
    used = count_scheduler_events(session, event_type, CAP_LEDGER_KEY, today)
    if used >= daily_cap:
        raise OutreachError(f"Daily cap reached ({used}/{daily_cap} {'follow-ups' if followup else 'first-contact emails'}).")

    if has_scheduler_event(session, event_type, str(contact.id), today):
        raise OutreachError("This contact was already emailed today; use /draft tomorrow for the follow-up.")

    sent_at = send_outreach_email(
        contact.email, subject, body, smtp_factory=smtp_factory, approval_token=SEND_APPROVAL_TOKEN
    )

    contact.updated_at = get_utc_now()
    contact.outreach_status = (
        OutreachStatus.SENT if not followup else OutreachStatus.FOLLOWED_UP
    )
    contact.sent_at = sent_at
    if not followup:
        followup_date = today + timedelta(days=settings.OUTREACH_FOLLOWUP_DAYS)
        contact.followup_at = followup_date.isoformat()
    session.add(contact)
    session.commit()
    session.refresh(contact)

    log_scheduler_event(session, event_type, CAP_LEDGER_KEY, today)
    log_scheduler_event(session, event_type, str(contact.id), today)
    return contact


def mark_contact_replied(session: Session, contact_id: int) -> Optional[Contact]:
    """Record that the contact replied (manual /replied reporting)."""
    contact = session.get(Contact, contact_id)
    if not contact:
        return None
    contact.email_status = EmailStatus.REPLIED
    contact.outreach_status = OutreachStatus.REPLIED
    contact.replied_at = get_utc_now()
    contact.updated_at = contact.replied_at
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact


def get_contacts_needing_followup(
    session: Session,
    ref_date: Optional[date] = None,
) -> List[Tuple[Contact, int]]:
    """Contacts whose follow-up date has arrived (status SENT, not yet followed up)."""
    today = ref_date or today_local()
    rows = session.exec(
        select(Contact).where(Contact.outreach_status == OutreachStatus.SENT)
    ).all()
    results: List[Tuple[Contact, int]] = []
    for contact in rows:
        if not contact.followup_at:
            continue
        try:
            due = date.fromisoformat(contact.followup_at)
        except ValueError:
            continue
        if due <= today:
            results.append((contact, (today - due).days))
    results.sort(key=lambda pair: -pair[1])
    return results


def upsert_contact(
    session: Session,
    full_name: str,
    company_name: Optional[str] = None,
    role_title: Optional[str] = None,
    relationship: str = "",
    email: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    github_url: Optional[str] = None,
    source: ContactSource = ContactSource.MANUAL,
    email_status: EmailStatus = EmailStatus.UNVERIFIED,
) -> Tuple[Contact, bool]:
    """Create a contact or merge new details into an existing duplicate match."""
    existing = None
    if email:
        existing = session.exec(select(Contact).where(Contact.email == email)).first()
    if existing is None and linkedin_url:
        existing = session.exec(select(Contact).where(Contact.linkedin_url == linkedin_url)).first()
    if existing is None and full_name and company_name:
        # Fallback for the NO_MATCH flow: a contact saved without an email must
        # still be found later when the user adds it by manual email address.
        norm_name = normalize_text_key(full_name)
        norm_company = normalize_text_key(company_name)
        if norm_name and norm_company:
            for candidate in session.exec(select(Contact)).all():
                if (
                    normalize_text_key(candidate.full_name or "") == norm_name
                    and normalize_text_key(candidate.company_name or "") == norm_company
                ):
                    existing = candidate
                    break

    if existing:
        existing.updated_at = get_utc_now()
        if company_name and not existing.company_name:
            existing.company_name = company_name
        if role_title and not existing.role_title:
            existing.role_title = role_title
        if relationship and not existing.relationship:
            existing.relationship = relationship
        if email and not existing.email:
            existing.email = email
        if linkedin_url and not existing.linkedin_url:
            existing.linkedin_url = linkedin_url
        if email_status == EmailStatus.PROSPEO_VERIFIED:
            existing.email_status = EmailStatus.PROSPEO_VERIFIED
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing, False

    contact = Contact(
        full_name=full_name or email or linkedin_url or "Unknown Contact",
        company_name=company_name,
        role_title=role_title,
        relationship=relationship,
        email=email,
        linkedin_url=linkedin_url,
        github_url=github_url,
        source=source,
        email_status=email_status,
    )
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact, True
