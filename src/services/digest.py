"""Daily Internship Intelligence Digest and Deadline Engine.

Aggregates:
- High-match opportunities tailored to candidate profile (VIT 2028 batch)
- Approaching application deadlines with countdown warnings
- Status tracking of active applications in progress
- Actionable priority recommendations for the candidate
"""

import html
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import List, Optional, Tuple
from sqlmodel import Session

from src.database import (
    get_expired_deadlines,
    get_jobs_by_status,
    get_upcoming_deadlines,
    list_jobs,
)
from src.models.candidate import CandidateProfile
from src.models.job import ApplicationStatus, EligibilityStatus, JobPosting
from src.services.profile import evaluate_job_match, get_candidate_profile


@dataclass
class DigestReport:
    """Consolidated daily intelligence report."""
    report_date: date
    high_matches: List[Tuple[JobPosting, float]] = field(default_factory=list)
    upcoming_deadlines: List[Tuple[JobPosting, int]] = field(default_factory=list)
    active_applications: List[JobPosting] = field(default_factory=list)
    expired_deadlines: List[JobPosting] = field(default_factory=list)
    action_items: List[str] = field(default_factory=list)


def build_daily_digest(
    session: Session,
    profile: Optional[CandidateProfile] = None,
    deadline_days: int = 7,
    match_threshold: float = 0.65,
    ref_date: Optional[date] = None,
) -> DigestReport:
    """Build a comprehensive daily digest of opportunities, deadlines, and active tracks."""
    today = ref_date or date.today()
    cand_profile = profile or get_candidate_profile()

    # 1. Evaluate open opportunities for candidate match
    unapplied_jobs = list_jobs(session, limit=100)
    high_matches: List[Tuple[JobPosting, float]] = []

    for job in unapplied_jobs:
        if job.application_status in [ApplicationStatus.NEW, ApplicationStatus.REVIEWED]:
            match_eval = evaluate_job_match(
                role_title=job.role_title,
                locations=job.locations,
                allowed_grad_years=job.allowed_grad_years,
                required_skills=job.required_skills,
                profile=cand_profile,
            )
            # Include if overall score passes threshold or explicitly eligible
            if match_eval.overall_match_score >= match_threshold or job.eligibility_status == EligibilityStatus.ELIGIBLE:
                high_matches.append((job, match_eval.overall_match_score))

    # Sort descending by match score
    high_matches.sort(key=lambda x: x[1], reverse=True)
    top_matches = high_matches[:5]

    # 2. Upcoming Deadlines (within deadline_days)
    deadlines = get_upcoming_deadlines(session, days_ahead=deadline_days, ref_date=today)
    upcoming_deadlines: List[Tuple[JobPosting, int]] = []
    for j in deadlines:
        if j.application_deadline:
            days_left = (j.application_deadline - today).days
            upcoming_deadlines.append((j, max(0, days_left)))

    # 3. Active Applications In-Flight (Applied / Interviewing / Offer)
    active_jobs = get_jobs_by_status(
        session,
        statuses=[ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER],
    )

    # 4. Expired Deadlines (for warning/archiving)
    expired = get_expired_deadlines(session, ref_date=today)

    # 5. Synthesize Action Items
    actions: List[str] = []

    # Priority 1: Closing soon (<= 3 days)
    urgent_deadlines = [d for d in upcoming_deadlines if d[1] <= 3]
    for u_job, u_days in urgent_deadlines:
        day_str = "today!" if u_days == 0 else (f"in {u_days} day" if u_days == 1 else f"in {u_days} days")
        actions.append(f"URGENT: Submit application for #{u_job.id} {u_job.company_name} - closing {day_str}!")

    # Priority 2: High Match Candidate
    if top_matches:
        best_job, best_score = top_matches[0]
        pct = int(best_score * 100)
        actions.append(f"Top Opportunity: #{best_job.id} {best_job.company_name} ({pct}% match) - Run `/tailor {best_job.id}` to generate resume & pitch.")

    # Priority 3: In-flight followups
    interviewing = [j for j in active_jobs if j.application_status == ApplicationStatus.INTERVIEWING]
    if interviewing:
        comp_names = ", ".join(j.company_name for j in interviewing[:3])
        actions.append(f"Interview Prep: Active rounds with {comp_names}. Review key system design and DSA.")

    if not actions:
        actions.append("All caught up! Forward internship links, screenshots, or flyer photos to ingest new roles.")

    return DigestReport(
        report_date=today,
        high_matches=top_matches,
        upcoming_deadlines=upcoming_deadlines,
        active_applications=active_jobs,
        expired_deadlines=expired,
        action_items=actions,
    )


def format_digest_html(report: DigestReport) -> str:
    """Format the digest into a clean, modern Telegram HTML message."""
    formatted_date = report.report_date.strftime("%A, %b %d, %Y")
    lines = [
        "🌅 <b>DAILY INTERNSHIP INTELLIGENCE BRIEFING</b>",
        f"📅 <i>{formatted_date}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # Section 1: Top Matched Opportunities
    if report.high_matches:
        lines.append("\n🌟 <b>Top Matched Opportunities:</b>")
        for job, score in report.high_matches:
            pct = int(score * 100)
            comp = html.escape(job.company_name)
            role = html.escape(job.role_title)
            batch = ", ".join(str(y) for y in job.allowed_grad_years) if job.allowed_grad_years else "Open Batch"
            lines.append(
                f"• <b>#{job.id} {comp}</b> — {role}\n"
                f"  <i>Score:</i> <b>{pct}%</b> | <i>Batch:</i> {html.escape(batch)} | <code>/tailor {job.id}</code>"
            )
    else:
        lines.append("\n🌟 <b>Top Matched Opportunities:</b>\n<i>No new matching roles yet. Ingest more links!</i>")

    # Section 2: Approaching Deadlines
    if report.upcoming_deadlines:
        lines.append("\n⏳ <b>Approaching Deadlines:</b>")
        for job, days_left in report.upcoming_deadlines:
            comp = html.escape(job.company_name)
            role = html.escape(job.role_title)
            d_str = job.application_deadline.strftime("%b %d") if job.application_deadline else "Soon"
            if days_left == 0:
                urgency = "🚨 <b>CLOSING TODAY</b>"
            elif days_left <= 2:
                urgency = f"⚠️ <b>{days_left}d remaining</b> ({d_str})"
            else:
                urgency = f"⏳ {days_left}d remaining ({d_str})"
            lines.append(f"• <b>#{job.id} {comp}</b> — {role} [{urgency}]")
    else:
        lines.append("\n⏳ <b>Approaching Deadlines:</b>\n<i>No immediate deadlines in the next 7 days.</i>")

    # Section 3: Active Applications In-Flight
    if report.active_applications:
        lines.append("\n📋 <b>Active Applications in Flight:</b>")
        for job in report.active_applications:
            comp = html.escape(job.company_name)
            role = html.escape(job.role_title)
            status_tag = job.application_status.value.upper()
            badge = "🗣️" if job.application_status == ApplicationStatus.INTERVIEWING else ("🎉" if job.application_status == ApplicationStatus.OFFER else "📨")
            lines.append(f"• {badge} <b>#{job.id} {comp}</b> — {role} [<code>{status_tag}</code>]")
    else:
        lines.append("\n📋 <b>Active Applications:</b>\n<i>No active applications currently submitted.</i>")

    # Section 4: Recommended Action Items
    if report.action_items:
        lines.append("\n⚡ <b>Recommended Action Today:</b>")
        for action in report.action_items:
            lines.append(f"<blockquote>{html.escape(action)}</blockquote>")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>Manage status:</i> <code>/status &lt;id&gt; &lt;applied|interviewing|archived&gt;</code>")
    return "\n".join(lines)


def format_digest_text(report: DigestReport) -> str:
    """Format the digest into plain text for CLI terminal display."""
    formatted_date = report.report_date.strftime("%A, %b %d, %Y")
    lines = [
        "============================================================",
        f"  DAILY INTERNSHIP INTELLIGENCE BRIEFING ({formatted_date})",
        "============================================================",
    ]

    lines.append("\n[*] Top Matched Opportunities:")
    if report.high_matches:
        for job, score in report.high_matches:
            pct = int(score * 100)
            batch = ", ".join(str(y) for y in job.allowed_grad_years) if job.allowed_grad_years else "Open Batch"
            lines.append(f"  - #{job.id} {job.company_name} | {job.role_title}")
            lines.append(f"    Match Score: {pct}% | Batch: {batch}")
            lines.append(f"    Command: python -m src.interfaces.cli tailor {job.id}")
    else:
        lines.append("  (No new matching opportunities found. Ingest new postings to populate.)")

    lines.append("\n[*] Approaching Deadlines:")
    if report.upcoming_deadlines:
        for job, days_left in report.upcoming_deadlines:
            d_str = job.application_deadline.strftime("%Y-%m-%d") if job.application_deadline else "Soon"
            tag = "TODAY!" if days_left == 0 else f"{days_left} day(s) left ({d_str})"
            lines.append(f"  - #{job.id} {job.company_name} - {job.role_title} [{tag}]")
    else:
        lines.append("  (No deadlines within the next 7 days.)")

    lines.append("\n[*] Active Applications in Flight:")
    if report.active_applications:
        for job in report.active_applications:
            status_tag = job.application_status.value.upper()
            lines.append(f"  - #{job.id} {job.company_name} - {job.role_title} [{status_tag}]")
    else:
        lines.append("  (No active applications currently submitted.)")

    lines.append("\n[*] Recommended Actions Today:")
    for action in report.action_items:
        lines.append(f"  -> {action}")

    lines.append("\n============================================================")
    return "\n".join(lines)
