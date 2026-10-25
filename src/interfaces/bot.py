"""Telegram Bot Interface for Personal Internship Intelligence Assistant.

Provides mobile ingestion and control:
- Handles shared links (scraped via scraper & ATS resolvers)
- Handles screenshots and camera uploads (multimodal Gemini Flash)
- Handles forwarded text announcements
- Handles PDF brochures
- Provides candidate profile inspection and project additions
- Tailors truthful ATS resumes & cold pitches via /tailor <job_id>
- Enforces strict single-user ID whitelisting
- Includes robust HTTP timeouts and resilient error handling
"""

import asyncio
import functools
import html
import logging
import re
from datetime import date, timedelta, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import List, Optional, Tuple

from sqlmodel import select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from src.config import settings
from src.database import (
    add_watch_target,
    deactivate_watch_target,
    get_job_by_id,
    get_jobs_needing_deadline_reminder,
    get_session,
    get_upcoming_deadlines,
    has_scheduler_event,
    init_db,
    list_jobs,
    list_watch_targets,
    log_scheduler_event,
    today_local,
    update_job_application_status,
)
from src.models.contact import Contact, OutreachStatus
from src.models.job import (
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    VerificationStatus,
)
from src.models.system import SchedulerEventType, WatchTargetType
from src.services.alumni_finder import AlumniFinderError, build_linkedin_search_urls, find_engineers_on_github
from src.services.contact_enrichment import (
    ProspeoError,
    enrich_linkedin_profile,
    enrichment_available,
)
from src.services.digest import (
    build_daily_digest,
    format_deadline_reminder_html,
    format_digest_html,
)
from src.services.discovery import (
    fetch_target_listings,
    is_alertable,
    is_internship_title,
    run_discovery,
)
from src.services.github_service import (
    extract_github_repo_coords,
    ingest_github_project,
    save_project_to_knowledge_base,
)
from src.services.outreach import (
    OutreachDraft,
    OutreachError,
    approve_and_send,
    craft_outreach_draft,
    get_contacts_needing_followup,
    mark_contact_replied,
    upsert_contact,
)
from src.services.pipeline import (
    process_image_pipeline,
    process_text_pipeline,
    process_url_pipeline,
)
from src.services.profile import (
    add_project_to_profile,
    get_candidate_profile,
    parse_project_from_text,
)
from src.services.resume import compute_resume_base_name, rebuild_tailored_resume, tailor_resume_for_job

logger = logging.getLogger(__name__)

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)"
)

# ==============================================================================
# Approved Cold-Outreach state (P1 Networking)
# ==============================================================================

# chat_id -> {"draft": OutreachDraft, "contact": Contact, "job": JobPosting}
_PENDING_DRAFTS: dict = {}
# chat_id -> {"contact_id": int, "job_id": int, "followup": bool} awaiting replacement text
_PENDING_DRAFT_EDITS: dict = {}
# chat_id -> {"linkedin_url": str, "name": str,...} awaiting an enriched Contact to confirm
_PENDING_ENRICHMENT: dict = {}


def is_authorized(user_id: Optional[int]) -> bool:
    """Check whether incoming user is whitelisted.

    Deny-by-default: if TELEGRAM_ALLOWED_USER_ID is unset (or the user is unknown),
    nobody may use the bot. This avoids strangers driving Gemini API spend and
    ingestion uploads on an unconfigured instance.
    """
    if user_id is None or not settings.TELEGRAM_ALLOWED_USER_ID:
        return False
    return user_id == settings.TELEGRAM_ALLOWED_USER_ID


async def _reject_unauthorized(update: Update) -> None:
    """Notify an unauthorized user (if in private chat) and log the attempt."""
    user = update.effective_user
    logger.warning(f"Blocked unauthorized Telegram access: {user.id if user else 'unknown'}")
    if update.callback_query:
        try:
            await update.callback_query.answer("⛔ Unauthorized. This is a private assistant instance.", show_alert=True)
        except Exception as exc:
            logger.debug(f"Could not answer unauthorized callback: {exc}")
    elif update.effective_message:
        try:
            await update.effective_message.reply_text("⛔ Unauthorized. This is a private assistant instance.")
        except Exception as exc:
            logger.debug(f"Could not notify unauthorized user: {exc}")


def requires_authorization(handler):
    """Guard a Telegram handler so it only executes for the whitelisted user."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not is_authorized(user.id if user else None):
            await _reject_unauthorized(update)
            return
        return await handler(update, context)
    return wrapper


def _sanitize_filename(raw: str) -> str:
    """Strip path traversal and unsafe characters from an uploaded filename (Telegram-side input)."""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\"<>|:*?]+", "_", name).strip().lstrip(".")
    return name[:100] or "document.pdf"


def get_job_inline_keyboard(job_id: int, application_url: Optional[str] = None) -> InlineKeyboardMarkup:
    """Generate interactive action buttons for a job card, including a direct 1-tap apply link if available."""
    rows = []
    if application_url and application_url.startswith(("http://", "https://")):
        rows.append([
            InlineKeyboardButton("🔗 Apply Directly", url=application_url)
        ])
    rows.append([
        InlineKeyboardButton("📄 Tailor Resume", callback_data=f"tailor:{job_id}"),
        InlineKeyboardButton("✅ Applied", callback_data=f"status:{job_id}:applied"),
    ])
    rows.append([
        InlineKeyboardButton("🗣️ Interviewing", callback_data=f"status:{job_id}:interviewing"),
        InlineKeyboardButton("📦 Archive", callback_data=f"status:{job_id}:archived"),
    ])
    return InlineKeyboardMarkup(rows)


def format_job_html_card(job: JobPosting) -> str:
    """Format a JobPosting into a clean, attractive Telegram HTML card."""
    safe_company = html.escape(job.company_name)
    safe_role = html.escape(job.role_title)
    locations_str = html.escape(", ".join(job.locations) if job.locations else "Not specified")
    batches_str = html.escape(", ".join(str(y) for y in job.allowed_grad_years) if job.allowed_grad_years else "Any / Open")
    deadline_str = html.escape(job.application_deadline.strftime("%b %d, %Y") if job.application_deadline else "Not stated")

    # Eligibility Badge
    if job.eligibility_status == EligibilityStatus.ELIGIBLE:
        el_badge = "🎯 <b>ELIGIBLE (2028 Batch)</b>"
    elif job.eligibility_status == EligibilityStatus.INELIGIBLE:
        el_badge = "🚫 <b>INELIGIBLE FOR 2028 BATCH</b>"
    else:
        el_badge = "❓ <b>ELIGIBILITY UNCERTAIN</b>"

    # Trust & Portal Badge
    if job.verification_status == VerificationStatus.SUSPICIOUS:
        trust_badge = "🚨 <b>SUSPICIOUS / POTENTIAL SCAM DETECTED</b>"
    elif job.is_official_portal:
        trust_badge = "✅ <b>Official Careers Portal / Verified ATS</b>"
    elif job.verification_status == VerificationStatus.UNVERIFIED_THIRD_PARTY:
        trust_badge = "⚠️ <i>Third-Party / Unverified Link</i>"
    else:
        trust_badge = "❓ <i>Unverified Source</i>"

    req_skills = html.escape(", ".join(job.required_skills) if job.required_skills else "None specified")
    pref_skills = html.escape(", ".join(job.preferred_skills) if job.preferred_skills else "None specified")

    lines = [
        f"📋 <b>JOB RECORD #{job.id}</b>",
        f"🏢 <b>Company:</b> {safe_company}",
        f"💼 <b>Role:</b> {safe_role}",
        f"📍 <b>Location:</b> {locations_str}",
        f"⏳ <b>Deadline:</b> {deadline_str}",
        f"🎓 <b>Batch:</b> {batches_str}",
        f"⚖️ <b>Verdict:</b> {el_badge}",
        f"   <i>{html.escape(job.eligibility_reason)}</i>",
        f"🛡️ <b>Trust:</b> {trust_badge}",
    ]

    if job.verification_source:
        lines.append(f"🔍 <i>{html.escape(job.verification_source)}</i>")

    if job.application_url:
        lines.append(f"🔗 <a href=\"{job.application_url}\"><b>Open Application Portal</b></a>")
    else:
        lines.append("🔗 <b>Application Link:</b> None provided in post")

    lines.extend([
        f"🛠️ <b>Required Skills:</b> <code>{req_skills}</code>",
        f"✨ <b>Preferred Skills:</b> <code>{pref_skills}</code>",
    ])

    if job.notes:
        lines.append(f"\n📊 <b>Profile Match Breakdown:</b>\n<i>{html.escape(job.notes)}</i>")

    lines.append(f"\n💡 <i>Generate tailored ATS resume & cold pitch:</i> <code>/tailor {job.id}</code>")

    return "\n".join(lines)


# ==============================================================================
# Telegram Command Handlers
# ==============================================================================

@requires_authorization
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and /help commands."""
    user = update.effective_user
    welcome_text = (
        f"👋 <b>Welcome back, {html.escape(user.first_name)}!</b>\n\n"
        "I am your <b>Personal AI Internship Intelligence Assistant</b>.\n\n"
        "<b>What you can send me anytime:</b>\n"
        "  🔗 <b>Job links</b> (Greenhouse, Lever, Ashby, company careers pages)\n"
        "  📸 <b>Screenshots & Flyers</b> from Instagram reels, LinkedIn, or WhatsApp\n"
        "  💬 <b>Forwarded text messages</b> announcing opportunities\n"
        "  📄 <b>PDF brochures</b> and campus hiring circulars\n\n"
        "<b>Available Commands:</b>\n"
        "/profile - View your candidate preferences & skills\n"
        "/addproject - Add a project to your portfolio and auto-sync skills\n"
        "/list - List recently saved opportunities\n"
        "/tailor &lt;id&gt; - Tailor an ATS PDF resume & cold outreach pitch\n"
        "/fsel &lt;id&gt; &lt;pick&gt; - Re-fit resume page fillers: <code>1,3</code> / <code>none</code> / <code>all</code>\n"
        "/status &lt;id&gt; &lt;status&gt; - Update job application status\n"
        "/deadlines - View approaching deadlines\n"
        "/digest - View daily intelligence briefing\n"
        "/watch - Manage automatic discovery boards (new!)\n"
        "/discover - Run a discovery sweep right now (new!)\n"
        "/addcontact &lt;email|-LinkedIn&gt; [| Name | Company | Role] - Track a networking contact\n"
        "/contacts [github &lt;company&gt;] - List contacts / run alumni visibility sweep\n"
        "/draft &lt;contact_id&gt; [job_id] - Draft an approval-gated cold intro email\n"
        "/replied &lt;contact_id&gt; - Mark that this contact replied\n"
        "/help - Show this guidance message\n\n"
        f"<i>Auto-scheduled:</i> 🌅 digest at {settings.DIGEST_HOUR_LOCAL:02d}:00 local"
        f" every day, ⏰ deadline reminders every {settings.DEADLINE_CHECK_INTERVAL_HOURS}h,"
        f" 🛰️ watchlist sweep every {settings.DISCOVERY_CHECK_INTERVAL_HOURS}h,"
        f" 🔁 outreach follow-ups every {settings.OUTREACH_CHECK_INTERVAL_HOURS}h."
    )
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /profile command."""
    p = get_candidate_profile()
    msg = (
        f"👤 <b>CANDIDATE PROFILE: {html.escape(p.full_name)}</b>\n"
        f"🏛️ <b>College:</b> {html.escape(p.college)}\n"
        f"🎓 <b>Degree:</b> {html.escape(p.degree)}\n"
        f"📅 <b>Current Year:</b> Year {p.current_college_year} (Graduation: {p.graduation_year})\n"
        f"🎯 <b>Target Tracks:</b> {html.escape(', '.join(p.target_roles))}\n"
        f"🌐 <b>Semester Availability:</b> {html.escape(p.current_availability)}\n"
        f"📍 <b>Summer Hubs:</b> {html.escape(', '.join(p.summer_locations))}\n"
        f"✈️ <b>Relocation:</b> <i>{html.escape(p.relocation_preference)}</i>\n\n"
        f"🛠️ <b>Verified Skills ({len(p.verified_skills)}):</b>\n"
        f"<code>{html.escape(', '.join(p.verified_skills))}</code>\n\n"
        f"📂 <b>Verified Projects ({len(p.verified_projects)}):</b>\n"
    )

    for idx, proj in enumerate(p.verified_projects, 1):
        msg += f"<b>{idx}. {html.escape(proj.title)}</b> ({html.escape(', '.join(proj.tech_stack))})\n"

    try:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    except TimedOut:
        logger.warning("Timed out while replying to /profile, retrying once...")
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_add_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addproject <text_or_github_url> command."""
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "?? <b>How to add a project:</b>\n\n"
            "Reply with: <code>/addproject &lt;project summary or GitHub URL&gt;</code>\n\n"
            "<i>Examples:</i>\n"
            "? <code>/addproject https://github.com/Punit-154/Reco.ai</code>\n"
            "? <code>/addproject Built a real-time log anomaly detector using Python, FastAPI, Docker, and AWS: github.com/punit/cloud-sentinel</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if extract_github_repo_coords(text):
        status_msg = await update.message.reply_text(
            "?? <b>Detected GitHub repository URL!</b>\n"
            "<i>Fetching raw README.md, extracting technical specs, and syncing profile...</i>",
            parse_mode=ParseMode.HTML,
        )
        try:
            project, new_skills, folder = await asyncio.to_thread(ingest_github_project, text)
        except Exception as exc:
            logger.error(f"Error ingesting GitHub project: {exc}")
            await status_msg.edit_text(f"? Failed to ingest GitHub project: {html.escape(str(exc))}")
            return
    else:
        status_msg = await update.message.reply_text("?? Analyzing and structuring project with Gemini...")
        try:
            project = await asyncio.to_thread(parse_project_from_text, text)
            folder = await asyncio.to_thread(save_project_to_knowledge_base, project)
            _, new_skills = await asyncio.to_thread(add_project_to_profile, project)
        except Exception as exc:
            logger.error(f"Error adding project via Telegram: {exc}")
            await status_msg.edit_text(f"? Failed to parse project: {html.escape(str(exc))}")
            return

    response = (
        f"? <b>Project Synchronized to Portfolio & KB:</b>\n"
        f"?? <b>{html.escape(project.title)}</b>\n"
        f"??? <b>Tech Stack:</b> {html.escape(', '.join(project.tech_stack))}\n"
        f"?? <i>\"{html.escape(project.description)}\"</i>\n"
    )
    if project.github_url:
        response += f"?? <a href=\"{project.github_url}\">GitHub Repository</a>\n"
    response += f"?? <b>Knowledge Base:</b> <code>data/projects/{folder.name}/README.md</code>\n"

    if new_skills:
        response += f"\n? <b>Auto-Added New Verified Skills:</b> <code>{html.escape(', '.join(new_skills))}</code>"

    await status_msg.edit_text(response, parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list command to show saved opportunities."""
    with get_session() as session:
        jobs = list_jobs(session, limit=10)
        if not jobs:
            await update.message.reply_text("📭 No internship opportunities tracked yet.")
            return

        lines = ["📋 <b>Recently Tracked Opportunities:</b>\n"]
        for j in jobs:
            batch = ", ".join(str(y) for y in j.allowed_grad_years) if j.allowed_grad_years else "Any"
            status_emoji = "🎯" if j.eligibility_status == EligibilityStatus.ELIGIBLE else ("🚫" if j.eligibility_status == EligibilityStatus.INELIGIBLE else "❓")
            lines.append(
                f"{status_emoji} <b>#{j.id} {html.escape(j.company_name)}</b> - {html.escape(j.role_title)} "
                f"({batch}) [{j.application_status.value.upper()}]"
            )

        lines.append("\n<i>Send any URL or image to add a new opportunity!</i>")
        lines.append("<i>Tailor a resume:</i> <code>/tailor &lt;id&gt;</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_tailor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /tailor <job_id> to generate a tailored ATS resume and cold outreach pitch."""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "💡 <b>Usage:</b> <code>/tailor &lt;job_id&gt;</code>\n\n"
            "<i>Example:</i> <code>/tailor 8</code> (tailors an ATS-optimized resume for Job #8)",
            parse_mode=ParseMode.HTML,
        )
        return

    job_id = int(args[0])
    status_msg = await update.message.reply_text(
        f"🤖 <b>Tailoring truthful ATS resume for Job #{job_id}...</b>\n"
        "<i>Matching verified skills, structuring STAR bullet points, and generating ATS PDF resume...</i>",
        parse_mode=ParseMode.HTML,
    )
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    with get_session() as session:
        job = get_job_by_id(session, job_id)
        if not job:
            await status_msg.edit_text(f"❌ Job record #{job_id} not found in database.")
            return

        profile = get_candidate_profile()

        try:
            tailored_res = await asyncio.to_thread(tailor_resume_for_job, profile, job)

            # Build summary message
            summary_html = (
                f"📄 <b>ATS-TAILORED RESUME GENERATED</b>\n\n"
                f"🏢 <b>Target:</b> {html.escape(job.company_name)} - {html.escape(job.role_title)}\n"
                f"🎯 <b>Match Rationale:</b>\n<i>{html.escape(tailored_res.match_rationale)}</i>\n\n"
                f"🤝 <b>LinkedIn Referral Outreach Pitch:</b>\n"
                f"<blockquote>{html.escape(tailored_res.linkedin_referral_pitch)}</blockquote>\n\n"
                f"✉️ <b>Cold Email Draft:</b>\n"
                f"<pre>{html.escape(tailored_res.cold_email_draft)}</pre>\n\n"
                f"📄 <i>Delivering ready-to-send ATS PDF Resume below:</i>"
            )

            await status_msg.edit_text(summary_html, parse_mode=ParseMode.HTML)

            # Deliver the ready-to-send ATS PDF as a Telegram Document
            if tailored_res.pdf_file_path and tailored_res.pdf_file_path.exists():
                with open(tailored_res.pdf_file_path, "rb") as pdf_f:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=pdf_f,
                        filename=tailored_res.pdf_file_path.name,
                        caption=f"📄 Ready-to-Send PDF Resume for {job.company_name}",
                    )

            # Space-filler review loop: rebuilds from the cached payload cost zero tokens
            if tailored_res.generated_filler_titles:
                filler_lines = "\n".join(
                    f"  <b>{i}.</b> {html.escape(t)}"
                    for i, t in enumerate(tailored_res.generated_filler_titles, start=1)
                )
                await update.message.reply_text(
                    "🧩 <b>Page-space filler candidates</b> (auto-stacked if the page allows):\n\n"
                    f"{filler_lines}\n\n"
                    f"Want control? Reply <code>/fsel {job_id} 1,3</code> or "
                    f"<code>/fsel {job_id} none</code> to re-fit the PDF with your pick.",
                    parse_mode=ParseMode.HTML,
                )

        except Exception as exc:
            logger.error(f"Error tailoring resume for job #{job_id}: {exc}")
            await status_msg.edit_text(
                f"❌ <b>Resume Tailoring Failed:</b>\n<i>{html.escape(str(exc))}</i>",
                parse_mode=ParseMode.HTML,
            )


@requires_authorization
async def cmd_fsel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /fsel <job_id> <pick> to refit the tailored PDF with a chosen filler subset."""
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "💡 <b>Usage:</b> <code>/fsel &lt;job_id&gt; &lt;pick&gt;</code>\n\n"
            "<i>Examples:</i>\n"
            "• <code>/fsel 8 1,3</code> — keep filler projects 1 and 3\n"
            "• <code>/fsel 8 none</code> — drop every filler project\n"
            "• <code>/fsel 8 all</code> — restore all generated fillers",
            parse_mode=ParseMode.HTML,
        )
        return

    spec = (args[1] if len(args) > 1 else "all").strip().lower()
    if spec == "all":
        keep_fillers = None
    elif spec == "none":
        keep_fillers = []
    elif spec.replace(",", "").isdigit():
        keep_fillers = [int(x) for x in spec.split(",") if x.strip().isdigit()]
    else:
        await update.message.reply_text(
            f"❌ Invalid pick: <code>{html.escape(spec)}</code>. Use ranks (e.g. <code>1,3</code>), "
            "<code>none</code>, or <code>all</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    job_id = int(args[0])
    with get_session() as session:
        job = get_job_by_id(session, job_id)
        if not job:
            await update.message.reply_text(f"❌ Job record #{job_id} not found in database.")
            return

        profile = get_candidate_profile()
        payload_path = Path("resumes") / f"{compute_resume_base_name(job)}_payload.json"

        try:
            rebuilt = await asyncio.to_thread(
                rebuild_tailored_resume, profile, job, payload_path, keep_fillers
            )
        except FileNotFoundError:
            await update.message.reply_text(
                f"❌ No cached tailoring payload for Job #{job_id}. Run <code>/tailor {job_id}</code> first.",
                parse_mode=ParseMode.HTML,
            )
            return
        except ValueError as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
            return

        kept = (
            f"{len(keep_fillers or rebuilt.generated_filler_titles)} of {len(rebuilt.generated_filler_titles)}"
            if rebuilt.generated_filler_titles
            else "0"
        )
        await update.message.reply_text(
            f"♻️ <b>Resume re-fitted</b> for {html.escape(job.company_name)}\n"
            f"🧩 Fillers in PDF: <b>{kept}</b>\n"
            "<i>Delivering updated PDF:</i>",
            parse_mode=ParseMode.HTML,
        )
        if rebuilt.pdf_file_path and rebuilt.pdf_file_path.exists():
            with open(rebuilt.pdf_file_path, "rb") as pdf_f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=pdf_f,
                    filename=rebuilt.pdf_file_path.name,
                    caption=f"📄 Updated PDF Resume for {job.company_name}",
                )


@requires_authorization
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status <job_id> <new_status> [notes] to update application status."""
    args = context.args
    if not args or len(args) < 2 or not args[0].isdigit():
        valid_opts = "new, reviewed, applied, interviewing, offer, rejected, archived"
        await update.message.reply_text(
            "📋 <b>Usage:</b> <code>/status &lt;job_id&gt; &lt;new_status&gt; [notes]</code>\n\n"
            f"<b>Allowed Statuses:</b> <code>{valid_opts}</code>\n\n"
            "<i>Examples:</i>\n"
            "• <code>/status 8 applied</code>\n"
            "• <code>/status 8 interviewing Round 1 Technical next Tuesday</code>\n"
            "• <code>/status 8 offer Received summer offer letter!</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    job_id = int(args[0])
    status_str = args[1].lower()
    notes = " ".join(args[2:]) if len(args) > 2 else None

    with get_session() as session:
        try:
            updated = update_job_application_status(session, job_id, status_str, notes=notes)
            if not updated:
                await update.message.reply_text(f"❌ Job record #{job_id} not found in database.")
                return

            response = (
                f"✅ <b>Status Updated for Job #{job_id}:</b>\n\n"
                f"🏢 <b>Company:</b> {html.escape(updated.company_name)}\n"
                f"💼 <b>Role:</b> {html.escape(updated.role_title)}\n"
                f"📊 <b>New Status:</b> <code>{updated.application_status.value.upper()}</code>\n"
            )
            if updated.notes:
                response += f"📝 <b>Notes:</b> <i>{html.escape(updated.notes)}</i>\n"

            await update.message.reply_text(
                response,
                parse_mode=ParseMode.HTML,
                reply_markup=get_job_inline_keyboard(updated.id, application_url=updated.application_url),
            )
        except ValueError as exc:
            await update.message.reply_text(f"❌ {html.escape(str(exc))}")


@requires_authorization
async def cmd_deadlines(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /deadlines command to list upcoming deadlines."""
    days_ahead = 14
    if context.args and context.args[0].isdigit():
        days_ahead = int(context.args[0])

    today = today_local()
    with get_session() as session:
        upcoming = get_upcoming_deadlines(session, days_ahead=days_ahead)
        if not upcoming:
            await update.message.reply_text(
                f"⏳ <b>No application deadlines approaching</b> in the next {days_ahead} days.\n"
                "<i>Your tracked pipeline has no immediate expirations.</i>",
                parse_mode=ParseMode.HTML,
            )
            return

        lines = [f"⏳ <b>Upcoming Deadlines (Next {days_ahead} Days):</b>\n"]
        for j in upcoming:
            days_left = (j.application_deadline - today).days
            comp = html.escape(j.company_name)
            role = html.escape(j.role_title)
            d_str = j.application_deadline.strftime("%b %d")
            if days_left == 0:
                badge = "🚨 <b>CLOSING TODAY</b>"
            elif days_left <= 2:
                badge = f"⚠️ <b>{days_left}d left ({d_str})</b>"
            else:
                badge = f"⏳ {days_left}d left ({d_str})"

            lines.append(f"• <b>#{j.id} {comp}</b> — {role}\n  [{badge}] | Status: <code>{j.application_status.value.upper()}</code>")

        lines.append("\n<i>Tailor an application:</i> <code>/tailor &lt;id&gt;</code>")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /digest command to generate the daily internship briefing on demand."""
    status_msg = await update.message.reply_text("🌅 <i>Synthesizing daily intelligence briefing...</i>", parse_mode=ParseMode.HTML)
    with get_session() as session:
        try:
            report = await asyncio.to_thread(build_daily_digest, session)
            digest_html = format_digest_html(report)
            await status_msg.edit_text(digest_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as exc:
            logger.error(f"Error generating digest: {exc}")
            await status_msg.edit_text(f"❌ Failed to build daily digest: {html.escape(str(exc))}")


# ==============================================================================
# Background Scheduler (Tier 1: real 8AM digest + deadline reminders + discovery)
# ==============================================================================

def get_target_chat_id() -> Optional[int]:
    """Resolve the private chat used for scheduled pushes (= whitelisted user id)."""
    return settings.TELEGRAM_ALLOWED_USER_ID


def _sync_build_digest_html() -> Optional[str]:
    """Blocking digest build for asyncio.to_thread (reads DB).

    Returns None when today's digest was already delivered (idempotency check);
    the ledger row is only written after the send succeeds, so a failed delivery
    is retried on the next run instead of being silently lost.
    """
    with get_session() as session:
        if has_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today_local()):
            return None
        report = build_daily_digest(session)
        return format_digest_html(report)


def _sync_log_digest_delivered() -> None:
    """Record today's daily digest as delivered (called only after a successful send)."""
    with get_session() as session:
        log_scheduler_event(session, SchedulerEventType.DAILY_DIGEST, "digest", today_local())


def _sync_collect_deadline_reminders() -> Tuple[List[Tuple[JobPosting, int]], date]:
    """Collect unapplied jobs closing soon; return pairs plus the 'today' date used for dedupe."""
    with get_session() as session:
        today = today_local()
        pairs = get_jobs_needing_deadline_reminder(
            session, window_days=settings.DEADLINE_REMINDER_WINDOW_DAYS, ref_date=today
        )
        # Materialize into plain tuples so ORM objects detached from this session stay usable.
        return [(job, days) for job, days in pairs], today


def _sync_mark_reminders_sent(pairs: List[Tuple[JobPosting, int]], sent_date: date) -> None:
    """Mark reminders as delivered using the same 'today' reference as the collect step."""
    with get_session() as session:
        for job, _days in pairs:
            log_scheduler_event(session, SchedulerEventType.DEADLINE_REMINDER, str(job.id), sent_date)


def _sync_run_discovery() -> Tuple[list, int]:
    """Blocking discovery run for asyncio.to_thread; returns (new outcomes, target count)."""
    with get_session() as session:
        targets = list_watch_targets(session, active_only=True)
        outcomes = run_discovery(session)
        return outcomes, len(targets)


def _sync_collect_outreach_followups() -> List[Tuple[int, int]]:
    """Collect contacts due for a follow-up; detach-safe (ids only)."""
    with get_session() as session:
        pairs = get_contacts_needing_followup(session)
        return [(contact.id, overdue) for contact, overdue in pairs]


def _sync_build_followup_draft(contact_id: int):
    """Blocking follow-up draft builder; returns (html, keyboard, chat-safe payload) or (None, None, None)."""
    with get_session() as session:
        contact = session.get(Contact, contact_id)
        if not contact:
            return None, None, None
        job = get_job_by_id(session, contact.last_job_id) if contact.last_job_id else None
        if not job:
            latest = session.exec(select(JobPosting).order_by(JobPosting.id.desc())).first()
            job = latest
        if not job:
            return None, None, None
        try:
            draft = craft_outreach_draft(contact, job, followup=True)
        except Exception as exc:
            logger.error(f"Follow-up draft failed for contact #{contact_id}: {exc}")
            return None, None, None
        card_html, keyboard = render_draft_card(draft, contact, job)
        payload = {"draft": draft, "contact_id": contact.id, "job_id": job.id}
        return card_html, keyboard, payload


async def scheduled_outreach_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: surface due follow-up drafts for approval (never auto-sends)."""
    if not settings.SCHEDULER_ENABLED:
        return
    chat_id = get_target_chat_id()
    if not chat_id:
        return
    try:
        due = await asyncio.to_thread(_sync_collect_outreach_followups)
        if not due:
            return
        for contact_id, overdue in due[:3]:
            card_html, keyboard, payload = await asyncio.to_thread(_sync_build_followup_draft, contact_id)
            if not card_html:
                continue
            _PENDING_DRAFTS[chat_id] = payload
            await context.bot.send_message(
                chat_id=chat_id,
                text=card_html,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        days_note = f" (oldest overdue by {due[0][1]}d)" if due and due[0][1] else ""
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔁 <b>Follow-up time:</b> {len(due)} contact(s) due{days_note}. Approval cards above — "
            "nothing sends until you tap ✅ Approve.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.error(f"[Scheduler] Outreach follow-up sweep failed: {exc}")


async def scheduled_daily_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: deliver the morning intelligence briefing (8AM local)."""
    if not settings.SCHEDULER_ENABLED:
        return
    chat_id = get_target_chat_id()
    if not chat_id:
        logger.warning("[Scheduler] Daily digest skipped: TELEGRAM_ALLOWED_USER_ID is unset.")
        return
    try:
        digest_html = await asyncio.to_thread(_sync_build_digest_html)
        if digest_html is None:
            logger.info("[Scheduler] Daily digest already delivered today; skipping.")
            return
        await context.bot.send_message(
            chat_id=chat_id,
            text=digest_html,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.to_thread(_sync_log_digest_delivered)
    except Exception as exc:
        logger.error(f"[Scheduler] Daily digest failed: {exc}")


async def scheduled_deadline_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: sweep for unapplied opportunities closing soon; push once per day."""
    if not settings.SCHEDULER_ENABLED:
        return
    chat_id = get_target_chat_id()
    if not chat_id:
        return
    try:
        pairs, reminder_today = await asyncio.to_thread(_sync_collect_deadline_reminders)
        if not pairs:
            return
        reminder_text = format_deadline_reminder_html(pairs)
        await context.bot.send_message(
            chat_id=chat_id,
            text=reminder_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.to_thread(_sync_mark_reminders_sent, pairs, reminder_today)
    except Exception as exc:
        logger.error(f"[Scheduler] Deadline reminder sweep failed: {exc}")


async def scheduled_discovery_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: poll watch targets and push alert-worthy new postings."""
    if not settings.SCHEDULER_ENABLED or not settings.DISCOVERY_ENABLED:
        return
    chat_id = get_target_chat_id()
    if not chat_id:
        logger.warning("[Scheduler] Discovery skipped: TELEGRAM_ALLOWED_USER_ID is unset.")
        return
    try:
        outcomes, target_count = await asyncio.to_thread(_sync_run_discovery)
        if not outcomes:
            return
        alertable = [o for o in outcomes if is_alertable(o)]
        silent = len(outcomes) - len(alertable)
        for outcome in alertable:
            job = outcome.job
            card = (
                "🛰️ <b>NEW OPPORTUNITY DISCOVERED</b>\n\n"
                f"{format_job_html_card(job)}"
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=card,
                parse_mode=ParseMode.HTML,
                reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url),
                disable_web_page_preview=True,
            )
        summary = (
            f"🛰️ Discovery sweep checked {target_count} watch target(s): "
            f"{len(alertable)} alert-worthy new posting(s) pushed above"
            + (f", {silent} lower-match posting(s) saved silently." if silent else ".")
        )
        await context.bot.send_message(chat_id=chat_id, text=summary, parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.error(f"[Scheduler] Discovery sweep failed: {exc}")


def schedule_background_jobs(application: Application) -> None:
    """Register steady-state background jobs on the PTB JobQueue.

    - Daily digest at DIGEST_HOUR_LOCAL (Kolkata tz by default)
    - Deadline reminder sweep every DEADLINE_CHECK_INTERVAL_HOURS
    - Watchlist discovery sweep every DISCOVERY_CHECK_INTERVAL_HOURS
    - Outreach follow-up sweep every OUTREACH_CHECK_INTERVAL_HOURS
    """
    if not settings.SCHEDULER_ENABLED:
        logger.info("[Scheduler] Background scheduling disabled (SCHEDULER_ENABLED=false).")
        return
    if application.job_queue is None:
        logger.warning(
            "[Scheduler] JobQueue unavailable — install the job-queue extra: "
            'pip install "python-telegram-bot[job-queue]"'
        )
        return

    local_tz = timezone(timedelta(hours=settings.TZ_OFFSET_HOURS))
    application.job_queue.run_daily(
        scheduled_daily_digest,
        time=dt_time(hour=settings.DIGEST_HOUR_LOCAL, tzinfo=local_tz),
        name="daily_digest",
    )
    application.job_queue.run_repeating(
        scheduled_deadline_check,
        interval=settings.DEADLINE_CHECK_INTERVAL_HOURS * 3600,
        first=180,
        name="deadline_sweep",
    )
    application.job_queue.run_repeating(
        scheduled_discovery_check,
        interval=settings.DISCOVERY_CHECK_INTERVAL_HOURS * 3600,
        first=90,
        name="discovery_sweep",
    )
    application.job_queue.run_repeating(
        scheduled_outreach_check,
        interval=settings.OUTREACH_CHECK_INTERVAL_HOURS * 3600,
        first=240,
        name="outreach_followup_sweep",
    )
    print(f"[*] Scheduler armed: digest at {settings.DIGEST_HOUR_LOCAL:02d}:00 local | "
          f"deadline sweep every {settings.DEADLINE_CHECK_INTERVAL_HOURS}h | "
          f"discovery sweep every {settings.DISCOVERY_CHECK_INTERVAL_HOURS}h | "
          f"outreach sweep every {settings.OUTREACH_CHECK_INTERVAL_HOURS}h")


# ==============================================================================
# Watchlist & Discovery Commands
# ==============================================================================

def _board_browser_url(target) -> str:
    """Human-browsable URL for a watch target (for display in /watch list)."""
    k = target.target_key
    return {
        WatchTargetType.ASHBY_BOARD: f"https://jobs.ashbyhq.com/{k}",
        WatchTargetType.GREENHOUSE_BOARD: f"https://boards.greenhouse.io/{k}",
        WatchTargetType.LEVER_BOARD: f"https://jobs.lever.co/{k}",
        WatchTargetType.CUSTOM_URL: k,
    }.get(target.target_type, k)


@requires_authorization
async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /watch add|list|remove — manage the ATS board discovery watchlist."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "🛰️ <b>Watchlist — Proactive Job Discovery</b>\n\n"
            "I automatically poll these boards and push new intern postings to you:\n\n"
            "<b>Usage:</b>\n"
            "• <code>/watch add ashby &lt;board_slug&gt; [Company]</code>\n"
            "• <code>/watch add greenhouse &lt;token&gt; [Company]</code>\n"
            "• <code>/watch add lever &lt;slug&gt; [Company]</code>\n"
            "• <code>/watch add url &lt;careers_page_url&gt; [Company]</code>\n"
            "• <code>/watch list</code> — show targets & status\n"
            "• <code>/watch remove &lt;id&gt;</code> — stop watching\n\n"
            "<b>Examples:</b>\n"
            "• <code>/watch add lever spotify</code>\n"
            "• <code>/watch add greenhouse stripe Stripe</code>\n"
            "• <code>/watch add ashby linear</code>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    action = args[0].lower()

    if action == "list":
        with get_session() as session:
            targets = list_watch_targets(session, active_only=False)
            if not targets:
                await update.message.reply_text("🛰️ Watchlist empty. Add one with <code>/watch add lever spotify</code>", parse_mode=ParseMode.HTML)
                return
            lines = ["🛰️ <b>Watch Targets:</b>\n"]
            active_targets = [t for t in targets if t.active]
            for t in targets:
                status_icon = "✅" if t.active else "🚫"
                checked = t.last_checked_at.strftime("%b %d %H:%M") if t.last_checked_at else "never"
                err = f" ⚠️ <i>{html.escape(t.last_error[:60])}</i>" if t.last_error else ""
                lines.append(
                    f"{status_icon} <b>#{t.id} {html.escape(t.company_name)}</b> "
                    f"(<code>{t.target_type.value.split('_')[0]}:{html.escape(t.target_key[:24])}</code>)\n"
                    f"    🔗 {_board_browser_url(t)}\n"
                    f"    🕒 last checked: {checked} UTC{err}"
                )
            lines.append(f"\nActive: <b>{len(active_targets)}</b> of {len(targets)} targets | Manual check: /discover")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    if action == "add":
        if len(args) < 3:
            await update.message.reply_text("💡 Usage: <code>/watch add &lt;ashby|greenhouse|lever|url&gt; &lt;key&gt; [Company]</code>", parse_mode=ParseMode.HTML)
            return
        watch_type, key = args[1].lower(), args[2].strip()
        company_label = " ".join(args[3:]).strip() or key.replace("-", " ").title()

        if watch_type in {"url", "custom"} and not key.startswith(("http://", "https://")):
            key = "https://" + key.lstrip("/")

        with get_session() as session:
            try:
                target = add_watch_target(session, company_label, watch_type, key)
            except ValueError as exc:
                await update.message.reply_text(f"❌ {html.escape(str(exc))}")
                return

            probe_msg = await update.message.reply_text(
                f"🛰️ <b>Target #{target.id} saved.</b> Verifying the board is reachable...",
                parse_mode=ParseMode.HTML,
            )
            try:
                listings = await asyncio.to_thread(fetch_target_listings, target)
                intern_count = sum(1 for l in listings if is_internship_title(l.title))
                await probe_msg.edit_text(
                    f"✅ <b>Watching {html.escape(target.company_name)}</b> (#{target.id})\n"
                    f"🔗 {_board_browser_url(target)}\n"
                    f"📊 Live postings on board right now: <b>{len(listings)}</b>"
                    + (f" (≈{intern_count} intern/trainee roles)" if intern_count else "")
                    + "\n\n<i>New internship postings will be pushed automatically. Manual check anytime: /discover</i>",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except Exception as exc:
                await probe_msg.edit_text(
                    f"⚠️ <b>Target #{target.id} saved, but the board check FAILED:</b>\n"
                    f"<code>{html.escape(str(exc)[:200])}</code>\n\n"
                    "<i>Double-check the slug/token (open the board in a browser). Discovery will keep"
                    " retrying in the background — /watch list shows the last error.</i>",
                    parse_mode=ParseMode.HTML,
                )
        return

    if action == "remove":
        if len(args) < 2 or not args[1].isdigit():
            await update.message.reply_text("💡 Usage: <code>/watch remove &lt;id&gt;</code> (see /watch list)", parse_mode=ParseMode.HTML)
            return
        target_id = int(args[1])
        with get_session() as session:
            target = deactivate_watch_target(session, target_id)
            if target:
                await update.message.reply_text(f"🚫 Stopped watching <b>{html.escape(target.company_name)}</b> (#{target.id}).", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text(f"❌ Watch target #{target_id} not found.", parse_mode=ParseMode.HTML)
        return

    await update.message.reply_text("❓ Unknown action. Use <code>/watch add|list|remove</code>.", parse_mode=ParseMode.HTML)


@requires_authorization
async def cmd_discover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /discover — run one discovery sweep across the watchlist right now."""
    status_msg = await update.message.reply_text(
        "🛰️ <i>Checking your watchlist boards for new internship postings...</i>",
        parse_mode=ParseMode.HTML,
    )
    try:
        outcomes, target_count = await asyncio.to_thread(_sync_run_discovery)
    except Exception as exc:
        logger.error(f"Discovery command failed: {exc}")
        await status_msg.edit_text(f"❌ Discovery failed: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        return

    alertable = [o for o in outcomes if is_alertable(o)]
    silent = len(outcomes) - len(alertable)

    if not outcomes:
        await status_msg.edit_text(
            f"🛰️ <b>Sweep complete:</b> {target_count} watch target(s) checked.\n"
            "<i>No new internship postings found right now.</i>",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    for outcome in alertable:
        job = outcome.job
        card = "🛰️ <b>NEW OPPORTUNITY DISCOVERED</b>\n\n" + format_job_html_card(job)
        await update.message.reply_text(
            card,
            parse_mode=ParseMode.HTML,
            reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url),
            disable_web_page_preview=True,
        )
    summary = (
        f"🛰️ <b>Sweep complete:</b> {len(outcomes)} new posting(s) ingested from "
        f"{target_count} watch target(s) — {len(alertable)} pushed above"
        + (f", {silent} lower-match saved silently (see /list)." if silent else ".")
    )
    await update.message.reply_text(summary, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ==============================================================================
# Approved Cold-Outreach Commands (P1 Networking)
# ==============================================================================

_OUTREACH_STATUS_ICONS = {
    OutreachStatus.NONE: "⚪",
    OutreachStatus.DRAFTED: "✍️",
    OutreachStatus.SENT: "📤",
    OutreachStatus.FOLLOWED_UP: "🔁",
    OutreachStatus.REPLIED: "📩",
}


def _format_contact_line(contact: Contact) -> str:
    icon = _OUTREACH_STATUS_ICONS.get(contact.outreach_status, "⚪")
    company = f" @ {html.escape(contact.company_name)}" if contact.company_name else ""
    role = f" ({html.escape(contact.role_title)})" if contact.role_title else ""
    relation = f" | {html.escape(contact.relationship)}" if contact.relationship else ""
    email_bit = f"\n    📧 <code>{html.escape(contact.email)}</code>" if contact.email else "\n    ⚠️ no email stored"
    li_bit = f"\n    🔗 {html.escape(contact.linkedin_url)}" if contact.linkedin_url else ""
    return f"{icon} <b>#{contact.id} {html.escape(contact.full_name)}</b>{company}{role}{relation}{email_bit}{li_bit}"


def render_draft_card(draft: OutreachDraft, contact: Contact, job: JobPosting):
    """Build the approval-card HTML + keyboard for a pending outreach draft."""
    contact_label = f"{contact.full_name}" + (f" @ {contact.company_name}" if contact.company_name else "")
    kind = "🔁 FOLLOW-UP" if draft.is_followup else "✍️ FIRST CONTACT"
    card_html = (
        f"📧 <b>COLD OUTREACH DRAFT</b> ({kind})\n\n"
        f"👤 <b>To:</b> {html.escape(contact_label)}"
        + (f" ({html.escape(contact.email)})" if contact.email else " <i>(no email!)</i>")
        + f"\n🎯 <b>Re:</b> {html.escape(job.company_name)} — {html.escape(job.role_title)}\n\n"
        f"<b>Subject:</b> {html.escape(draft.subject)}\n\n"
        f"<pre>{html.escape(draft.body)}</pre>\n\n"
        "<i>✅ Approve sends via your Gmail. ✏️ Edit replaces the text. 🗑 Discard deletes it.</i>"
    )
    keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Approve & Send", callback_data=f"outreach:approve:{contact.id}"),
            InlineKeyboardButton("✏️ Edit", callback_data=f"outreach:edit:{contact.id}"),
            InlineKeyboardButton("🗑 Discard", callback_data=f"outreach:discard:{contact.id}"),
        ]]
    )
    return card_html, keyboard


def _sync_latest_job_id(session) -> Optional[int]:
    row = session.exec(select(JobPosting.id).order_by(JobPosting.id.desc())).first()
    return row if row else None


@requires_authorization
async def cmd_addcontact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /addcontact <email-or-linkedin> [| Full Name [| Company [| Role [| relationship]]]]."""
    raw = " ".join(context.args or []).strip()
    if not raw:
        enrich_state = "ON (Prospeo)" if enrichment_available() else "OFF (set PROSPEO_API_KEY in .env)"
        await update.message.reply_text(
            "🤝 <b>Add a networking contact</b>\n\n"
            "<b>Usage:</b>\n"
            "• <code>/addcontact recruiter@company.com | Jane Doe | Google | Recruiter | recruiter</code>\n"
            "• <code>/addcontact https://linkedin.com/in/janedoe</code> "
            "— auto-enriches email via Prospeo\n\n"
            "Only the first field is required; later fields fill blanks on duplicate. "
            f"Prospeo enrichment: <b>{enrich_state}</b>. List: /contacts",
            parse_mode=ParseMode.HTML,
        )
        return

    parts = [p.strip() for p in raw.split("|")]
    identifier = parts[0]
    full_name = parts[1] if len(parts) > 1 and parts[1] else ""
    company_name = parts[2] if len(parts) > 2 and parts[2] else None
    role_title = parts[3] if len(parts) > 3 and parts[3] else None
    relationship = parts[4] if len(parts) > 4 and parts[4] else ""

    if "linkedin.com/in/" in identifier:
        if not enrichment_available():
            await update.message.reply_text(
                "❌ LinkedIn lookup needs <code>PROSPEO_API_KEY</code> in .env (Starter plan gives API access).\n"
                "💡 Until then add contacts by email: <code>/addcontact jane@company.com | Jane | ...</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        status_msg = await update.message.reply_text("🛰️ <i>Enriching profile via Prospeo (1 credit)...</i>", parse_mode=ParseMode.HTML)
        try:
            enriched = await asyncio.to_thread(enrich_linkedin_profile, identifier)
        except ProspeoError as exc:
            await status_msg.edit_text(
                f"❌ <b>Enrichment failed:</b>\n<i>{html.escape(str(exc))}</i>\n\n"
                "Try adding by email instead: <code>/addcontact jane@company.com | Jane</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        with get_session() as session:
            contact, created = upsert_contact(
                session,
                full_name=enriched.full_name or full_name,
                company_name=enriched.company_name or company_name,
                role_title=enriched.role_title or role_title,
                relationship=relationship,
                email=enriched.email or None,
                linkedin_url=identifier,
                source="prospeo",
                email_status="prospeo_verified" if enriched.email else "unverified",
            )
        await status_msg.edit_text(
            f"✅ <b>Contact {'saved' if created else 'updated'}: {html.escape(contact.full_name)}</b>\n"
            f"📧 {html.escape(contact.email or '(no email found)')} | 🏢 {html.escape(contact.company_name or '—')}\n"
            f"💼 {html.escape(contact.role_title or '—')}\n\n"
            "<i>Draft an intro:</i> <code>/draft " + str(contact.id) + "</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if "@" not in identifier or identifier.startswith("@"):
        await update.message.reply_text(
            "❌ First field must be an email or LinkedIn URL.\n"
            "<code>/addcontact recruiter@company.com | Jane Doe | Company | Role | recruiter</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    with get_session() as session:
        contact, created = upsert_contact(
            session,
            full_name=full_name,
            company_name=company_name,
            role_title=role_title,
            relationship=relationship,
            email=identifier,
            source="manual",
            email_status="unverified",
        )
    await update.message.reply_text(
        f"✅ <b>Contact {'saved' if created else 'updated'}: {html.escape(contact.full_name)}</b>\n"
        + _format_contact_line(contact)
        + "\n\n<i>Draft an intro:</i> <code>/draft " + str(contact.id) + "</code>",
        parse_mode=ParseMode.HTML,
    )


@requires_authorization
async def cmd_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /contacts [github <company>] — list tracked contacts or run a GitHub sweep."""
    args = context.args or []
    if args and args[0].lower() == "github":
        if len(args) < 2:
            await update.message.reply_text(
                "💡 Usage: <code>/contacts github &lt;company&gt;</code> — e.g. <code>/contacts github Stripe</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        company = " ".join(args[1:])
        status_msg = await update.message.reply_text(
            f"🛰️ <i>Searching public GitHub for engineers at {html.escape(company)}...</i>", parse_mode=ParseMode.HTML
        )
        try:
            engineers = await asyncio.to_thread(find_engineers_on_github, company)
        except AlumniFinderError as exc:
            await status_msg.edit_text(f"❌ GitHub sweep failed: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
            return
        urls = build_linkedin_search_urls(company)
        lines = [f"🛰️ <b>{html.escape(company)} — VIT-alumn Visibility Report</b>\n"]
        if engineers:
            lines.append("<b>Engineers with GitHub presence:</b>")
            for eng in engineers:
                lines.append(f"• <b>{html.escape(eng.full_name)}</b> — 🔗 {html.escape(eng.github_url)}")
        else:
            lines.append("<i>No public GitHub hits (most recruiters/alumni don't list code profiles).</i>")
        lines.append("\n<b>LinkedIn pre-search links (paste results → /addcontact):</b>")
        lines.append(f"• VIT alumni at {html.escape(company)}: {html.escape(urls['alumni'])}")
        lines.append(f"• Recruiters at {html.escape(company)}: {html.escape(urls['recruiters'])}")
        lines.append(f"• Engineers at {html.escape(company)}: {html.escape(urls['engineers'])}")
        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return

    with get_session() as session:
        contacts = session.exec(select(Contact).order_by(Contact.id.desc())).all()
    if not contacts:
        await update.message.reply_text(
            "🤝 <b>Networking book is empty.</b>\nAdd one: <code>/addcontact jane@company.com | Jane | Company | Role | recruiter</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    lines = ["🤝 <b>Networking Contacts:</b>\n"]
    for contact in contacts:
        lines.append(_format_contact_line(contact))
    sent = sum(1 for c in contacts if c.outreach_status in (OutreachStatus.SENT, OutreachStatus.FOLLOWED_UP))
    replied = sum(1 for c in contacts if c.outreach_status == OutreachStatus.REPLIED)
    lines.append(
        f"\nTotal: <b>{len(contacts)}</b> | emailed: <b>{sent}</b> | replied: <b>{replied}</b>\n"
        "<i>Draft: /draft &lt;id&gt; | Mark reply: /replied &lt;id&gt; | Sweep: /contacts github &lt;company&gt;</i>"
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@requires_authorization
async def cmd_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /draft <contact_id> [job_id] — generate an approval-gated cold email draft."""
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "💡 Usage: <code>/draft &lt;contact_id&gt; [job_id]</code>\n"
            "<i>Job defaults to your most recently ingested posting. List contacts: /contacts</i>",
            parse_mode=ParseMode.HTML,
        )
        return
    contact_id = int(args[0])
    chat_id = update.effective_chat.id

    with get_session() as session:
        contact = session.get(Contact, contact_id)
        if not contact:
            await update.message.reply_text(f"❌ Contact #{contact_id} not found. List: /contacts")
            return
        if contact.freeze_followups:
            await update.message.reply_text(
                f"🧊 Follow-ups are frozen for {html.escape(contact.full_name)}. Clear <code>freeze_followups</code> in the contacts table to unfreeze."
            )
            return
        if not contact.email and not enrichment_available():
            await update.message.reply_text(
                f"❌ {html.escape(contact.full_name)} has no email stored. "
                "Add <code>PROSPEO_API_KEY</code> + <code>/addcontact &lt;linkedin&gt;</code>, or re-add with an email.",
                parse_mode=ParseMode.HTML,
            )
            return
        job_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
        job = get_job_by_id(session, job_id) if job_id else None
        if not job:
            latest_id = _sync_latest_job_id(session)
            job = get_job_by_id(session, latest_id) if latest_id else None
        if not job:
            await update.message.reply_text("❌ No jobs in the pipeline yet — send a posting link first.")
            return
        job_copy = job

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    status_msg = await update.message.reply_text("✍️ <i>Drafting a truthful cold intro with Gemini...</i>", parse_mode=ParseMode.HTML)
    try:
        draft = await asyncio.to_thread(craft_outreach_draft, contact, job_copy)
    except Exception as exc:
        logger.error(f"Draft generation failed for contact #{contact_id}: {exc}")
        await status_msg.edit_text(f"❌ Drafting failed: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)
        return

    card_html, keyboard = render_draft_card(draft, contact, job_copy)
    _PENDING_DRAFTS[chat_id] = {"draft": draft, "contact_id": contact.id, "job_id": job_copy.id}
    await status_msg.edit_text(card_html, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)


@requires_authorization
async def cmd_replied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /replied <contact_id> — mark that this contact answered."""
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("💡 Usage: <code>/replied &lt;contact_id&gt;</code> (list: /contacts)")
        return
    with get_session() as session:
        contact = mark_contact_replied(session, int(args[0]))
    if contact:
        await update.message.reply_text(
            f"🎉 <b>Reply logged from {html.escape(contact.full_name)}</b> — keep notes in the Contact card."
            "\n<i>Next: prepare talking points before responding.</i>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❌ Contact #{args[0]} not found.")


@requires_authorization
async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle interactive button clicks from job cards."""
    query = update.callback_query

    data = query.data
    if not data:
        return

    if data.startswith("status:"):
        parts = data.split(":")
        if len(parts) >= 3 and parts[1].isdigit():
            job_id = int(parts[1])
            new_status = parts[2]
            with get_session() as session:
                try:
                    updated = update_job_application_status(session, job_id, new_status)
                    if updated:
                        await query.answer(f"Updated #{job_id} to {new_status.upper()}!")
                        card_html = format_job_html_card(updated)
                        await query.edit_message_text(
                            card_html,
                            parse_mode=ParseMode.HTML,
                            reply_markup=get_job_inline_keyboard(updated.id, application_url=updated.application_url),
                            disable_web_page_preview=True,
                        )
                    else:
                        await query.answer(f"Job #{job_id} not found.", show_alert=True)
                except Exception as exc:
                    logger.error(f"Error handling callback query status: {exc}")
                    await query.answer(f"Error: {exc}", show_alert=True)

    elif data.startswith("tailor:"):
        parts = data.split(":")
        if len(parts) >= 2 and parts[1].isdigit():
            job_id = int(parts[1])
            await query.answer("Generating tailored resume & outreach pitch...")
            with get_session() as session:
                job = get_job_by_id(session, job_id)
                if not job:
                    await query.message.reply_text(f"❌ Job record #{job_id} not found.")
                    return

                profile = get_candidate_profile()
                try:
                    tailored_res = await asyncio.to_thread(tailor_resume_for_job, profile, job)
                    matched_kws = ', '.join(tailored_res.ats_audit.matched_keywords) or 'None'
                    missing_kws = ', '.join(tailored_res.ats_audit.missing_keywords) or 'None'

                    summary_html = (
                        f"🎯 <b>ATS-TAILORED RESUME GENERATED</b>\n\n"
                        f"🏢 <b>Target:</b> {html.escape(job.company_name)} - {html.escape(job.role_title)}\n"
                        f"📊 <b>ATS Match Score:</b> <b>{tailored_res.ats_audit.match_percentage}%</b>\n"
                        f"✅ <b>Matched ATS Keywords:</b> <code>{html.escape(matched_kws)}</code>\n"
                        f"⚠️ <b>Missing Skill Gaps (Omitted):</b> <i>{html.escape(missing_kws)}</i>\n\n"
                        f"💡 <b>Match Rationale:</b>\n<i>{html.escape(tailored_res.match_rationale)}</i>\n\n"
                        f"🤝 <b>LinkedIn Referral Outreach Pitch:</b>\n"
                        f"<blockquote>{html.escape(tailored_res.linkedin_referral_pitch)}</blockquote>\n\n"
                        f"📧 <b>Cold Email Draft:</b>\n"
                        f"<pre>{html.escape(tailored_res.cold_email_draft)}</pre>\n\n"
                        f"📄 <i>Delivering ready-to-send ATS PDF Resume below:</i>"
                    )
                    await query.message.reply_text(summary_html, parse_mode=ParseMode.HTML)
                    if tailored_res.pdf_file_path and tailored_res.pdf_file_path.exists():
                        with open(tailored_res.pdf_file_path, "rb") as pdf_f:
                            await context.bot.send_document(
                                chat_id=update.effective_chat.id,
                                document=pdf_f,
                                filename=tailored_res.pdf_file_path.name,
                                caption=f"📄 Ready-to-Send ATS PDF Resume for {job.company_name}",
                            )
                except Exception as exc:
                    logger.error(f"Error tailoring resume from callback #{job_id}: {exc}")
                    await query.message.reply_text(f"❌ Tailoring failed: {html.escape(str(exc))}")

    elif data.startswith("outreach:"):
        parts = data.split(":")  # outreach:approve|edit|discard:<contact_id>
        action = parts[1] if len(parts) > 1 else ""
        contact_id = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        chat_id = update.effective_chat.id
        pending = _PENDING_DRAFTS.get(chat_id)

        if action == "approve":
            if not pending or pending.get("contact_id") != contact_id:
                await query.answer("This draft is no longer in memory — run /draft again.", show_alert=True)
                return
            draft = pending["draft"]
            try:
                with get_session() as session:
                    updated_contact = await asyncio.to_thread(
                        approve_and_send, session, contact_id, draft.subject, draft.body, draft.is_followup
                    )
            except OutreachError as exc:
                await query.answer(f"❌ {exc}", show_alert=True)
                return
            _PENDING_DRAFTS.pop(chat_id, None)
            followup_date = today_local() + timedelta(days=settings.OUTREACH_FOLLOWUP_DAYS)
            await query.answer("📧 Email sent!")
            await query.edit_message_text(
                f"✅ <b>Email sent to {html.escape(updated_contact.full_name)}</b>\n"
                f"🔁 <i>Follow-up card will surface on/after {followup_date.strftime('%b %d')} "
                "(scheduler sweeps every " + str(settings.OUTREACH_CHECK_INTERVAL_HOURS) + "h) — nothing auto-sends.</i>",
                parse_mode=ParseMode.HTML,
            )

        elif action == "discard":
            _PENDING_DRAFTS.pop(chat_id, None)
            _PENDING_DRAFT_EDITS.pop(chat_id, None)
            try:
                await query.answer("🗑 Draft discarded.")
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception as exc:
                logger.debug(f"Discard cleanup: {exc}")

        elif action == "edit":
            if not pending or pending.get("contact_id") != contact_id:
                await query.answer("This draft is no longer in memory — run /draft again.", show_alert=True)
                return
            _PENDING_DRAFT_EDITS[chat_id] = {"contact_id": contact_id, "job_id": pending.get("job_id"), "followup": pending["draft"].is_followup}
            await query.answer()
            await query.message.reply_text(
                "✏️ <b>Paste the replacement email now</b> — first line becomes the subject, the rest is the body.",
                parse_mode=ParseMode.HTML,
            )


# ==============================================================================
# Message Ingestion Handlers (URLs, Plain Text, Screenshots, PDFs)
# ==============================================================================

@requires_authorization
async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process incoming text messages, auto-detecting URLs vs raw job forwards."""
    text = update.message.text.strip()
    if not text:
        return

    chat_id = update.effective_chat.id

    # Outreach draft-edit interception: the next text replaces a pending draft.
    pending_edit = _PENDING_DRAFT_EDITS.get(chat_id)
    if pending_edit:
        lines = text.splitlines()
        if len(lines) < 2 or not lines[0].strip() or not text[len(lines[0]):].strip():
            await update.message.reply_text(
                "⚠️ Needed: <b>first line = subject</b>, then a blank line or body line, e.g.\n"
                "<code>Interested in the Data Intern role\n\nHi Sarah, ...</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        subject, body = lines[0].strip(), text[len(lines[0]):].strip()
        _PENDING_DRAFT_EDITS.pop(chat_id, None)
        pending = _PENDING_DRAFTS.get(chat_id)
        if pending and pending.get("contact_id") == pending_edit["contact_id"]:
            draft = pending["draft"]
            draft.subject, draft.body = subject, body
            with get_session() as session:
                contact = session.get(Contact, pending_edit["contact_id"])
                job = get_job_by_id(session, pending_edit["job_id"])
            if contact and job:
                card_html, keyboard = render_draft_card(draft, contact, job)
                await update.message.reply_text(card_html, parse_mode=ParseMode.HTML, reply_markup=keyboard, disable_web_page_preview=True)
                return
        await update.message.reply_text("❌ Original draft was lost — run <code>/draft &lt;contact_id&gt;</code> again.", parse_mode=ParseMode.HTML)
        return

    url_match = URL_REGEX.search(text)

    if url_match:
        target_url = url_match.group(0)
        status_msg = await update.message.reply_text(
            f"🔍 <b>Scraping and analyzing job portal:</b>\n<code>{html.escape(target_url[:60])}...</code>",
            parse_mode=ParseMode.HTML,
        )
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        with get_session() as session:
            try:
                job = await asyncio.to_thread(
                    process_url_pipeline,
                    session=session,
                    url=target_url,
                    source_type=IngestionSource.TELEGRAM_URL,
                )
                card_html = format_job_html_card(job)
                await status_msg.edit_text(card_html, parse_mode=ParseMode.HTML, reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url), disable_web_page_preview=True)
            except Exception as exc:
                logger.error(f"URL pipeline error: {exc}")
                await status_msg.edit_text(f"❌ <b>Processing Failed:</b>\n<i>{html.escape(str(exc))}</i>", parse_mode=ParseMode.HTML)

    else:
        status_msg = await update.message.reply_text("🤖 <b>Extracting job details from message text...</b>", parse_mode=ParseMode.HTML)
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

        with get_session() as session:
            try:
                job = await asyncio.to_thread(
                    process_text_pipeline,
                    session=session,
                    raw_text=text,
                    source_type=IngestionSource.TELEGRAM_TEXT,
                )
                card_html = format_job_html_card(job)
                await status_msg.edit_text(card_html, parse_mode=ParseMode.HTML, reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url))
            except Exception as exc:
                logger.error(f"Text pipeline error: {exc}")
                await status_msg.edit_text(f"❌ <b>Extraction Failed:</b>\n<i>{html.escape(str(exc))}</i>", parse_mode=ParseMode.HTML)


@requires_authorization
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download screenshot/photo from Telegram and run multimodal extraction."""
    photos = update.message.photo
    if not photos:
        return

    largest_photo = photos[-1]
    status_msg = await update.message.reply_text("📥 <b>Downloading screenshot for Multimodal Vision analysis...</b>", parse_mode=ParseMode.HTML)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_PHOTO)

    try:
        tg_file = await largest_photo.get_file()
        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        local_image_path = settings.UPLOAD_DIR / f"tg_{largest_photo.file_unique_id}.jpg"
        await tg_file.download_to_drive(custom_path=local_image_path)

        await status_msg.edit_text("🔍 <b>Analyzing visual flyer with Gemini Flash...</b>", parse_mode=ParseMode.HTML)

        with get_session() as session:
            job = await asyncio.to_thread(
                process_image_pipeline,
                session=session,
                file_path=local_image_path,
                source_type=IngestionSource.TELEGRAM_IMAGE,
            )
            card_html = format_job_html_card(job)
            await status_msg.edit_text(card_html, parse_mode=ParseMode.HTML, reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url))

    except Exception as exc:
        logger.error(f"Photo pipeline error: {exc}")
        await status_msg.edit_text(f"❌ <b>Image Extraction Failed:</b>\n<i>{html.escape(str(exc))}</i>", parse_mode=ParseMode.HTML)


@requires_authorization
async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download PDF or uncompressed document and run multimodal extraction."""
    doc = update.message.document
    if not doc:
        return

    # Sanitize the user-controlled filename before using it on disk.
    safe_filename = _sanitize_filename(doc.file_name or "document.pdf")
    status_msg = await update.message.reply_text(f"📥 <b>Downloading {html.escape(safe_filename)}...</b>", parse_mode=ParseMode.HTML)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    try:
        tg_file = await doc.get_file()
        settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        local_doc_path = settings.UPLOAD_DIR / f"tg_{doc.file_unique_id}_{safe_filename}"
        await tg_file.download_to_drive(custom_path=local_doc_path)

        await status_msg.edit_text("🔍 <b>Extracting job specifications with Gemini...</b>", parse_mode=ParseMode.HTML)

        with get_session() as session:
            job = await asyncio.to_thread(
                process_image_pipeline,
                session=session,
                file_path=local_doc_path,
                source_type=IngestionSource.TELEGRAM_IMAGE,
            )
            card_html = format_job_html_card(job)
            await status_msg.edit_text(card_html, parse_mode=ParseMode.HTML, reply_markup=get_job_inline_keyboard(job.id, application_url=job.application_url))

    except Exception as exc:
        logger.error(f"Document pipeline error: {exc}")
        await status_msg.edit_text(f"❌ <b>Document Extraction Failed:</b>\n<i>{html.escape(str(exc))}</i>", parse_mode=ParseMode.HTML)


# ==============================================================================
# Global Error Handler & Bot Application Builder
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any uncaught exception and prevent the poller daemon from dying."""
    err = context.error
    if isinstance(err, TimedOut):
        logger.warning(f"Telegram network request timed out (transient latency): {err}")
    elif isinstance(err, NetworkError):
        logger.warning(f"Telegram network glitch encountered: {err}")
    else:
        logger.error(f"Unhandled exception in Telegram update: {err}", exc_info=err)


def build_telegram_app() -> Application:
    """Construct and configure the Telegram application with robust timeouts and handlers."""
    token = settings.TELEGRAM_BOT_TOKEN
    if not token or token.startswith("123456789:ABC"):
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is missing or placeholder in .env file.\n"
            "Please obtain a token from @BotFather on Telegram and configure it in .env"
        )

    t_request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
        connection_pool_size=16,
    )

    app = Application.builder().token(token).request(t_request).build()

    # Register error handler
    app.add_error_handler(error_handler)

    # Commands
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("addproject", cmd_add_project))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("tailor", cmd_tailor))
    app.add_handler(CommandHandler("fsel", cmd_fsel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("deadlines", cmd_deadlines))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("discover", cmd_discover))
    app.add_handler(CommandHandler("addcontact", cmd_addcontact))
    app.add_handler(CommandHandler("contacts", cmd_contacts))
    app.add_handler(CommandHandler("draft", cmd_draft))
    app.add_handler(CommandHandler("replied", cmd_replied))

    # Inline Keyboard Button Clicks
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Media and messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))

    # Ensure auxiliary tables (scheduler_log, watch_targets) exist before jobs run
    if not settings.is_testing():
        init_db()

    # Background scheduler: 8AM digest + deadline reminders + discovery sweeps
    schedule_background_jobs(app)

    return app


def run_telegram_bot() -> None:
    """Start the Telegram bot using long polling with the background scheduler armed."""
    print("[*] Starting Telegram Bot with long polling...")
    whitelist_state = settings.TELEGRAM_ALLOWED_USER_ID or "LOCKED (no user whitelisted — set TELEGRAM_ALLOWED_USER_ID in .env)"
    print(f"[*] Whitelisted Telegram User ID: {whitelist_state}")
    print("[*] Resilient HTTP timeouts: 30s connect / 30s read")
    print("[*] Resume Tailoring enabled (/tailor <id>)")
    print("[*] Approved cold-outreach enabled (/addcontact /contacts /draft /replied)")
    print("[*] Background jobs: 8AM digest | deadline reminders | watchlist discovery | outreach follow-ups")
    print("    Manage boards with /watch | manual sweep with /discover")
    app = build_telegram_app()
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    run_telegram_bot()
