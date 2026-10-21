"""Database Engine, Sessions, and Table Initialization.

Provides thread-safe SQLite connection management, deduplication, and repository utilities.
"""

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Generator, List, Optional, Tuple, Union
from sqlalchemy import func
from sqlmodel import Session, SQLModel, create_engine, select
from src.config import settings
from src.models.job import ApplicationStatus, IngestionSource, JobPosting, RawIngestion
from src.models.system import SchedulerEventType, SchedulerLog, WatchTarget, WatchTargetType


def get_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def get_engine(db_url: Optional[str] = None):
    url = db_url or settings.DATABASE_URL
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=False, connect_args=connect_args)


engine = get_engine()


def init_db(target_engine=None) -> None:
    e = target_engine or engine
    SQLModel.metadata.create_all(e)


@contextmanager
def get_session(target_engine=None) -> Generator[Session, None, None]:
    e = target_engine or engine
    with Session(e) as session:
        yield session


# ==============================================================================
# Deduplication & Data Access Layer
# ==============================================================================

def normalize_text_key(text: str) -> str:
    """Normalize string for fuzzy-matching company and role titles."""
    return "".join(c for c in text.lower() if c.isalnum())


def find_existing_job(
    session: Session,
    company_name: str,
    role_title: str,
) -> Optional[JobPosting]:
    """Search for an existing job posting by company and role."""
    norm_comp = normalize_text_key(company_name)
    norm_role = normalize_text_key(role_title)

    # 1. Exact case-insensitive match
    stmt = select(JobPosting).where(
        func.lower(JobPosting.company_name) == company_name.strip().lower(),
        func.lower(JobPosting.role_title) == role_title.strip().lower(),
    )
    exact_match = session.exec(stmt).first()
    if exact_match:
        return exact_match

    # 2. Normalized alphanumeric check
    all_jobs = session.exec(select(JobPosting)).all()
    for j in all_jobs:
        if normalize_text_key(j.company_name) == norm_comp and normalize_text_key(j.role_title) == norm_role:
            return j

    return None


def save_raw_ingestion(
    session: Session,
    source_type: IngestionSource,
    raw_content: Optional[str] = None,
    media_path: Optional[str] = None,
) -> RawIngestion:
    record = RawIngestion(
        source_type=source_type,
        raw_content=raw_content,
        media_path=media_path,
        processed=False,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def save_or_update_job_posting(
    session: Session,
    job: JobPosting,
) -> Tuple[JobPosting, bool]:
    """Persist a job posting, merging into an existing record if company and role match.
    
    Returns:
        (job_record, is_new: bool)
    """
    existing = find_existing_job(session, job.company_name, job.role_title)
    if not existing:
        # Brand new job
        session.add(job)
        session.commit()
        session.refresh(job)
        return job, True

    # Duplicate detected: Smart merge new details into existing record
    existing.updated_at = get_utc_now()
    
    if not existing.application_url and job.application_url:
        existing.application_url = job.application_url
    if not existing.application_deadline and job.application_deadline:
        existing.application_deadline = job.application_deadline
    if not existing.is_official_portal and job.is_official_portal:
        existing.is_official_portal = job.is_official_portal
        existing.verification_status = job.verification_status
        existing.verification_source = job.verification_source

    # Merge locations
    merged_locs = list(dict.fromkeys(existing.locations + job.locations))
    existing.locations = merged_locs

    # Merge skills
    merged_req_skills = list(dict.fromkeys(existing.required_skills + job.required_skills))
    existing.required_skills = merged_req_skills
    merged_pref_skills = list(dict.fromkeys(existing.preferred_skills + job.preferred_skills))
    existing.preferred_skills = merged_pref_skills

    # Update notes
    if job.notes:
        existing.notes = job.notes

    session.add(existing)
    session.commit()
    session.refresh(existing)
    return existing, False


def save_job_posting(session: Session, job: JobPosting) -> JobPosting:
    """Convenience wrapper for backwards compatibility."""
    saved, _ = save_or_update_job_posting(session, job)
    return saved


def get_job_by_id(session: Session, job_id: int) -> Optional[JobPosting]:
    return session.get(JobPosting, job_id)


def list_jobs(
    session: Session,
    status: Optional[ApplicationStatus] = None,
    limit: int = 50,
) -> List[JobPosting]:
    statement = select(JobPosting)
    if status:
        statement = statement.where(JobPosting.application_status == status)
    statement = statement.order_by(JobPosting.created_at.desc()).limit(limit)
    return list(session.exec(statement).all())


def update_job_application_status(
    session: Session,
    job_id: int,
    new_status: Union[ApplicationStatus, str],
    notes: Optional[str] = None,
) -> Optional[JobPosting]:
    """Update the status of a job posting, accepting enum or string."""
    job = session.get(JobPosting, job_id)
    if not job:
        return None

    if isinstance(new_status, str):
        cleaned = new_status.strip().lower()
        valid_map = {s.value.lower(): s for s in ApplicationStatus}
        if cleaned not in valid_map:
            valid_keys = ", ".join(valid_map.keys())
            raise ValueError(f"Invalid status '{new_status}'. Allowed statuses: {valid_keys}")
        status_enum = valid_map[cleaned]
    else:
        status_enum = new_status

    job.application_status = status_enum
    job.updated_at = get_utc_now()
    if notes is not None:
        job.notes = notes

    session.add(job)
    session.commit()
    session.refresh(job)
    return job


def get_upcoming_deadlines(
    session: Session,
    days_ahead: int = 7,
    ref_date: Optional[date] = None,
) -> List[JobPosting]:
    """Return active opportunities closing within the next `days_ahead` days."""
    today = ref_date or date.today()
    cutoff = today + timedelta(days=days_ahead)

    statement = (
        select(JobPosting)
        .where(
            JobPosting.application_deadline != None,
            JobPosting.application_deadline >= today,
            JobPosting.application_deadline <= cutoff,
            JobPosting.application_status.not_in([ApplicationStatus.ARCHIVED, ApplicationStatus.REJECTED]),
        )
        .order_by(JobPosting.application_deadline.asc())
    )
    return list(session.exec(statement).all())


def get_expired_deadlines(
    session: Session,
    ref_date: Optional[date] = None,
) -> List[JobPosting]:
    """Return unapplied opportunities whose deadline has passed."""
    today = ref_date or date.today()

    statement = (
        select(JobPosting)
        .where(
            JobPosting.application_deadline != None,
            JobPosting.application_deadline < today,
            JobPosting.application_status.in_([ApplicationStatus.NEW, ApplicationStatus.REVIEWED]),
        )
        .order_by(JobPosting.application_deadline.desc())
    )
    return list(session.exec(statement).all())


def get_jobs_by_status(
    session: Session,
    statuses: List[ApplicationStatus],
    limit: int = 50,
) -> List[JobPosting]:
    """Return jobs matching any of the specified statuses."""
    statement = (
        select(JobPosting)
        .where(JobPosting.application_status.in_(statuses))
        .order_by(JobPosting.updated_at.desc())
        .limit(limit)
    )
    return list(session.exec(statement).all())


def purge_duplicate_jobs(session: Session) -> int:
    """One-time utility to deduplicate existing records in the database."""
    all_jobs = session.exec(select(JobPosting).order_by(JobPosting.id.asc())).all()
    seen = {}
    purged_count = 0

    for job in all_jobs:
        key = (normalize_text_key(job.company_name), normalize_text_key(job.role_title))
        if key in seen:
            session.delete(job)
            purged_count += 1
        else:
            seen[key] = job.id

    if purged_count > 0:
        session.commit()
    return purged_count


# ==============================================================================
# Scheduler Log (idempotency ledger for background jobs)
# ==============================================================================

def has_scheduler_event(
    session: Session,
    event_type: SchedulerEventType,
    event_key: str,
    sent_date: date,
) -> bool:
    """Check whether a background event was already delivered on a given date."""
    stmt = select(SchedulerLog).where(
        SchedulerLog.event_type == event_type,
        SchedulerLog.event_key == event_key,
        SchedulerLog.sent_date == sent_date,
    )
    return session.exec(stmt).first() is not None


def log_scheduler_event(
    session: Session,
    event_type: SchedulerEventType,
    event_key: str,
    sent_date: date,
    detail: Optional[str] = None,
) -> None:
    """Record that a background event was delivered on a date (idempotency marker)."""
    if not has_scheduler_event(session, event_type, event_key, sent_date):
        session.add(SchedulerLog(
            event_type=event_type,
            event_key=event_key,
            sent_date=sent_date,
            detail=detail,
        ))
        session.commit()


# ==============================================================================
# Deadline Reminder Sweep
# ==============================================================================

def get_jobs_needing_deadline_reminder(
    session: Session,
    window_days: int = 2,
    ref_date: Optional[date] = None,
) -> List[Tuple[JobPosting, int]]:
    """Return unapplied (NEW/REVIEWED) jobs closing within `window_days`, with days left.

    Jobs already reminded today are filtered out via the scheduler log.
    """
    today = ref_date or date.today()
    cutoff = today + timedelta(days=window_days)

    statement = (
        select(JobPosting)
        .where(
            JobPosting.application_deadline != None,
            JobPosting.application_deadline >= today,
            JobPosting.application_deadline <= cutoff,
            JobPosting.application_status.in_([ApplicationStatus.NEW, ApplicationStatus.REVIEWED]),
        )
        .order_by(JobPosting.application_deadline.asc())
    )
    results: List[Tuple[JobPosting, int]] = []
    for job in session.exec(statement).all():
        if has_scheduler_event(session, SchedulerEventType.DEADLINE_REMINDER, str(job.id), today):
            continue
        results.append((job, max(0, (job.application_deadline - today).days)))
    return results


# ==============================================================================
# Watch Target CRUD (Proactive Discovery)
# ==============================================================================

_VALID_WATCH_TYPES = {t.value: t for t in WatchTargetType}
_WATCH_TYPE_ALIASES = {
    "ashby": WatchTargetType.ASHBY_BOARD,
    "greenhouse": WatchTargetType.GREENHOUSE_BOARD,
    "gh": WatchTargetType.GREENHOUSE_BOARD,
    "lever": WatchTargetType.LEVER_BOARD,
    "url": WatchTargetType.CUSTOM_URL,
    "custom": WatchTargetType.CUSTOM_URL,
}


def add_watch_target(
    session: Session,
    company_name: str,
    target_type: Union[WatchTargetType, str],
    target_key: str,
    notes: Optional[str] = None,
) -> WatchTarget:
    """Register a new ATS board / careers URL under discovery surveillance."""
    if isinstance(target_type, str):
        cleaned = target_type.strip().lower().replace("-", "_")
        resolved = _WATCH_TYPE_ALIASES.get(cleaned) or _VALID_WATCH_TYPES.get(cleaned)
        if not resolved:
            raise ValueError(
                f"Invalid watch type '{target_type}'. "
                f"Allowed shorthands: {', '.join(_WATCH_TYPE_ALIASES)}"
            )
        target_type = resolved

    if not company_name or not company_name.strip():
        raise ValueError("Company name is required for a watch target.")
    if not target_key or not target_key.strip():
        raise ValueError("Target key (board slug or URL) is required for a watch target.")

    target = WatchTarget(
        company_name=company_name.strip(),
        target_type=target_type,
        target_key=target_key.strip(),
        active=True,
        notes=notes,
    )
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


def list_watch_targets(session: Session, active_only: bool = True) -> List[WatchTarget]:
    """Return registered watch targets, optionally filtering to active only."""
    statement = select(WatchTarget).order_by(WatchTarget.id.asc())
    targets = list(session.exec(statement).all())
    if active_only:
        targets = [t for t in targets if t.active]
    return targets


def deactivate_watch_target(session: Session, target_id: int) -> Optional[WatchTarget]:
    """Soft-delete a watch target (keeps history; discovery skips deactivated ones)."""
    target = session.get(WatchTarget, target_id)
    if not target:
        return None
    target.active = False
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


def touch_watch_target(
    session: Session,
    target_id: int,
    error: Optional[str] = None,
) -> Optional[WatchTarget]:
    """Record a completed poll attempt against a watch target (success or error)."""
    target = session.get(WatchTarget, target_id)
    if not target:
        return None
    target.last_checked_at = get_utc_now()
    target.last_error = error
    session.add(target)
    session.commit()
    session.refresh(target)
    return target
