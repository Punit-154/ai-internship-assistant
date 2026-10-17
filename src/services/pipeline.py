"""Unified Ingestion & Extraction Pipeline.

Coordinates scraping, multimodal image processing, LLM extraction, database audit logging,
profile-driven eligibility, and Milestone 5 opportunity verification / scam detection.
"""

import logging
import mimetypes
import shutil
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse
from sqlmodel import Session
from src.config import settings
from src.database import save_job_posting, save_raw_ingestion
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    RawIngestion,
    VerificationStatus,
)
from src.services.extractor import (
    ExtractedJobData,
    extract_job_from_image,
    extract_job_from_text,
)
from src.services.profile import evaluate_job_match
from src.services.scraper import scrape_url
from src.services.verifier import (
    TRUSTED_ATS_DOMAINS,
    VerificationResult,
    search_official_career_portal,
    verify_opportunity,
)

logger = logging.getLogger(__name__)

SUPPORTED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/webp",
    "application/pdf",
}


def parse_date_safe(date_str: Optional[str]) -> Optional[date]:
    """Safely convert YYYY-MM-DD string to date object, ignoring malformed strings."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def verify_portal_authenticity(
    url: Optional[str],
    company_name: str,
    role_title: str = "",
    raw_text: str = "",
) -> Tuple[bool, VerificationStatus, str]:
    """Inspect application URL or run search grounding to verify authenticity."""
    res = verify_opportunity(
        company_name=company_name,
        role_title=role_title,
        url=url,
        raw_text=raw_text,
    )
    return res.is_official, res.verification_status, res.notes


def evaluate_batch_eligibility(
    allowed_grad_years: list[int],
    target_year: int = 2028,
) -> Tuple[EligibilityStatus, str]:
    """Evaluate candidate graduation batch against opportunity requirements."""
    if not allowed_grad_years:
        return (
            EligibilityStatus.UNCERTAIN,
            "No specific graduation batch restricted. Open to all students meeting criteria.",
        )

    if target_year in allowed_grad_years:
        return (
            EligibilityStatus.ELIGIBLE,
            f"Eligible: Posting accepts {target_year} batch (Allowed: {allowed_grad_years}).",
        )

    return (
        EligibilityStatus.INELIGIBLE,
        f"Ineligible: Restricted to {allowed_grad_years}. Does not accept {target_year} batch.",
    )


def process_url_pipeline(
    session: Session,
    url: str,
    source_type: IngestionSource = IngestionSource.CLI_INPUT,
) -> JobPosting:
    """Ingest, scrape, extract, verify, and store a job posting from a URL."""
    raw_record = save_raw_ingestion(
        session=session,
        source_type=source_type,
        raw_content=url,
    )

    scrape_result = scrape_url(url)
    if not scrape_result.success:
        raw_record.error_message = scrape_result.error_message
        session.add(raw_record)
        session.commit()
        raise RuntimeError(f"Web scraping failed: {scrape_result.error_message}")

    try:
        extracted = extract_job_from_text(
            text=scrape_result.text,
            source_url=scrape_result.final_url,
        )
    except Exception as exc:
        raw_record.error_message = f"LLM Extraction failed: {str(exc)}"
        session.add(raw_record)
        session.commit()
        raise

    app_url = extracted.application_url or scrape_result.final_url
    verif = verify_opportunity(
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        url=app_url,
        raw_text=scrape_result.text,
    )
    
    if verif.official_portal_url:
        app_url = verif.official_portal_url

    match_eval = evaluate_job_match(
        role_title=extracted.role_title,
        locations=extracted.locations,
        allowed_grad_years=extracted.allowed_grad_years,
        required_skills=extracted.required_skills,
    )

    notes_parts = []
    if verif.red_flags:
        notes_parts.append(f"⚠️ RED FLAGS: {'; '.join(verif.red_flags)}")
    notes_parts.append(
        f"Match Score: {int(match_eval.overall_match_score * 100)}% | "
        f"Location: {match_eval.location_fit} | "
        f"Matching Skills: {', '.join(match_eval.matching_skills) or 'None'} | "
        f"Skill Gaps: {', '.join(match_eval.missing_skills) or 'None'}"
    )
    notes = " | ".join(notes_parts)

    job = JobPosting(
        raw_ingestion_id=raw_record.id,
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        locations=extracted.locations,
        application_deadline=parse_date_safe(extracted.application_deadline),
        application_url=app_url,
        allowed_grad_years=extracted.allowed_grad_years,
        eligibility_status=match_eval.batch_status,
        eligibility_reason=match_eval.batch_reason,
        required_skills=extracted.required_skills,
        preferred_skills=extracted.preferred_skills,
        verification_status=verif.verification_status,
        verification_source=verif.notes,
        is_official_portal=verif.is_official,
        application_status=ApplicationStatus.NEW,
        notes=notes,
    )

    saved_job = save_job_posting(session, job)
    raw_record.processed = True
    session.add(raw_record)
    session.commit()

    return saved_job


def process_text_pipeline(
    session: Session,
    raw_text: str,
    source_type: IngestionSource = IngestionSource.CLI_INPUT,
) -> JobPosting:
    """Ingest, extract, verify, and store a job posting from raw text or a message forward."""
    raw_record = save_raw_ingestion(
        session=session,
        source_type=source_type,
        raw_content=raw_text,
    )

    try:
        extracted = extract_job_from_text(text=raw_text)
    except Exception as exc:
        raw_record.error_message = f"LLM Extraction failed: {str(exc)}"
        session.add(raw_record)
        session.commit()
        raise

    orig_url = extracted.application_url
    extracted, app_url, gaps_filled = enrich_job_with_live_search(extracted)

    verif = verify_opportunity(
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        url=app_url,
        raw_text=raw_text,
    )
    if verif.official_portal_url:
        app_url = verif.official_portal_url

    match_eval = evaluate_job_match(
        role_title=extracted.role_title,
        locations=extracted.locations,
        allowed_grad_years=extracted.allowed_grad_years,
        required_skills=extracted.required_skills,
    )

    notes_parts = []
    if verif.red_flags:
        notes_parts.append(f"⚠️ RED FLAGS: {'; '.join(verif.red_flags)}")
    if gaps_filled:
        notes_parts.append(f"✨ Gaps Filled: Synced full specs & requirements from live portal ({app_url})")
    elif app_url and not orig_url:
        notes_parts.append(f"🔗 Application link discovered via search grounding: {app_url}")

    notes_parts.append(
        f"Match Score: {int(match_eval.overall_match_score * 100)}% | "
        f"Location: {match_eval.location_fit} | "
        f"Matching Skills: {', '.join(match_eval.matching_skills) or 'None'} | "
        f"Skill Gaps: {', '.join(match_eval.missing_skills) or 'None'}"
    )
    notes = " | ".join(notes_parts)

    job = JobPosting(
        raw_ingestion_id=raw_record.id,
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        locations=extracted.locations,
        application_deadline=parse_date_safe(extracted.application_deadline),
        application_url=app_url,
        allowed_grad_years=extracted.allowed_grad_years,
        eligibility_status=match_eval.batch_status,
        eligibility_reason=match_eval.batch_reason,
        required_skills=extracted.required_skills,
        preferred_skills=extracted.preferred_skills,
        verification_status=verif.verification_status,
        verification_source=verif.notes,
        is_official_portal=verif.is_official,
        application_status=ApplicationStatus.NEW,
        notes=notes,
    )

    saved_job = save_job_posting(session, job)
    raw_record.processed = True
    session.add(raw_record)
    session.commit()

    return saved_job


def enrich_job_with_live_search(
    extracted: ExtractedJobData,
) -> Tuple[ExtractedJobData, Optional[str], bool]:
    """Search for authentic live application link if missing and scrape to fill information gaps.
    
    Returns:
        (enriched_extracted_data, application_url, gaps_filled_flag)
    """
    app_url = extracted.application_url
    if not app_url or not app_url.startswith(("http://", "https://")):
        try:
            discovered_url = search_official_career_portal(extracted.company_name, extracted.role_title)
            if discovered_url:
                app_url = discovered_url
                extracted.application_url = discovered_url
                logger.info(f"Discovered live application link via search: {app_url}")
        except Exception as exc:
            logger.warning(f"Live portal search grounding failed: {exc}")

    # Check if the extracted data has information gaps
    has_missing_skills = len(extracted.required_skills) < 3
    has_missing_batches = not extracted.allowed_grad_years
    has_missing_deadline = not extracted.application_deadline
    has_missing_locations = not extracted.locations
    has_gaps = has_missing_skills or has_missing_batches or has_missing_deadline or has_missing_locations

    gaps_filled = False
    if app_url and app_url.startswith(("http://", "https://")) and has_gaps:
        try:
            logger.info(f"Attempting live scraping of {app_url} to fill job detail gaps...")
            scrape_res = scrape_url(app_url, timeout=10.0)
            if scrape_res.success and len(scrape_res.text.strip()) >= 50:
                live_data = extract_job_from_text(scrape_res.text)

                # Merge required skills (case-insensitive deduplication)
                existing_req_lower = {s.lower() for s in extracted.required_skills}
                for s in live_data.required_skills:
                    if s.lower() not in existing_req_lower:
                        extracted.required_skills.append(s)
                        existing_req_lower.add(s.lower())

                # Merge preferred skills
                existing_pref_lower = {s.lower() for s in extracted.preferred_skills}
                for s in live_data.preferred_skills:
                    if s.lower() not in existing_pref_lower and s.lower() not in existing_req_lower:
                        extracted.preferred_skills.append(s)
                        existing_pref_lower.add(s.lower())

                # Fill batches / grad years
                if not extracted.allowed_grad_years and live_data.allowed_grad_years:
                    extracted.allowed_grad_years = live_data.allowed_grad_years

                # Fill deadline
                if not extracted.application_deadline and live_data.application_deadline:
                    extracted.application_deadline = live_data.application_deadline

                # Fill locations
                if not extracted.locations and live_data.locations:
                    extracted.locations = live_data.locations

                gaps_filled = True
                logger.info(f"Successfully enriched job details from live portal {app_url}")
        except Exception as exc:
            logger.warning(f"Error scraping live portal to fill gaps: {exc}")

    return extracted, app_url, gaps_filled


def process_image_pipeline(
    session: Session,
    file_path: Path,
    source_type: IngestionSource = IngestionSource.CLI_INPUT,
) -> JobPosting:
    """Ingest, persist media, run multimodal extraction, and store structured job from screenshot/flyer."""
    file_path = Path(file_path).resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"Media file not found at: {file_path}")

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type or mime_type.lower() not in SUPPORTED_MIME_TYPES:
        ext = file_path.suffix.lower()
        if ext in {".jpg", ".jpeg"}:
            mime_type = "image/jpeg"
        elif ext == ".png":
            mime_type = "image/png"
        elif ext == ".webp":
            mime_type = "image/webp"
        elif ext == ".pdf":
            mime_type = "application/pdf"
        else:
            raise ValueError(
                f"Unsupported file format '{ext}'. Please upload an image (.png, .jpg, .webp) or PDF (.pdf)."
            )

    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest_filename = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{file_path.suffix}"
    dest_path = settings.UPLOAD_DIR / dest_filename
    if file_path.parent != settings.UPLOAD_DIR:
        shutil.copyfile(file_path, dest_path)
    else:
        dest_path = file_path

    raw_record = save_raw_ingestion(
        session=session,
        source_type=source_type,
        media_path=str(dest_path),
    )

    try:
        image_bytes = dest_path.read_bytes()
        extracted = extract_job_from_image(image_bytes=image_bytes, mime_type=mime_type)
    except Exception as exc:
        raw_record.error_message = f"Multimodal Extraction failed: {str(exc)}"
        session.add(raw_record)
        session.commit()
        raise

    orig_url = extracted.application_url
    extracted, app_url, gaps_filled = enrich_job_with_live_search(extracted)

    verif = verify_opportunity(
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        url=app_url,
        raw_text=f"{extracted.company_name} {extracted.role_title} {' '.join(extracted.required_skills)}",
    )
    if verif.official_portal_url:
        app_url = verif.official_portal_url

    match_eval = evaluate_job_match(
        role_title=extracted.role_title,
        locations=extracted.locations,
        allowed_grad_years=extracted.allowed_grad_years,
        required_skills=extracted.required_skills,
    )

    notes_parts = []
    if verif.red_flags:
        notes_parts.append(f"⚠️ RED FLAGS: {'; '.join(verif.red_flags)}")
    if gaps_filled:
        notes_parts.append(f"✨ Gaps Filled: Synced full specs & requirements from live portal ({app_url})")
    elif app_url and not orig_url:
        notes_parts.append(f"🔗 Application link discovered via search grounding: {app_url}")

    notes_parts.append(
        f"Match Score: {int(match_eval.overall_match_score * 100)}% | "
        f"Location: {match_eval.location_fit} | "
        f"Matching Skills: {', '.join(match_eval.matching_skills) or 'None'} | "
        f"Skill Gaps: {', '.join(match_eval.missing_skills) or 'None'}"
    )
    notes = " | ".join(notes_parts)

    job = JobPosting(
        raw_ingestion_id=raw_record.id,
        company_name=extracted.company_name,
        role_title=extracted.role_title,
        locations=extracted.locations,
        application_deadline=parse_date_safe(extracted.application_deadline),
        application_url=app_url,
        allowed_grad_years=extracted.allowed_grad_years,
        eligibility_status=match_eval.batch_status,
        eligibility_reason=match_eval.batch_reason,
        required_skills=extracted.required_skills,
        preferred_skills=extracted.preferred_skills,
        verification_status=verif.verification_status,
        verification_source=verif.notes,
        is_official_portal=verif.is_official,
        application_status=ApplicationStatus.NEW,
        notes=notes,
    )

    saved_job = save_job_posting(session, job)
    raw_record.processed = True
    session.add(raw_record)
    session.commit()

    return saved_job
