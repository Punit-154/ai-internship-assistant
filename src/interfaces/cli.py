"""Local PowerShell CLI Interface for Administration, Inspection, Profile Management, and Extraction.

Provides fast, scriptable access to database initialization, candidate profile updates,
project addition, and direct extraction testing (with live Gemini or offline mock mode),
as well as launching the Telegram bot listener.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src.database import get_session, init_db, list_jobs, save_job_posting
from src.models.candidate import ProjectItem
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    IngestionSource,
    JobPosting,
    VerificationStatus,
)
from src.services.extractor import ExtractedJobData
from src.services.pipeline import (
    process_image_pipeline,
    process_text_pipeline,
    process_url_pipeline,
)
from src.services.profile import (
    add_project_to_profile,
    get_candidate_profile,
    parse_project_from_text,
    update_candidate_profile,
)
from src.services.scraper import ScrapeResult


def print_job_card(job: JobPosting):
    """Render a clean, formatted card for a job posting in the terminal."""
    batches = ", ".join(str(y) for y in job.allowed_grad_years) if job.allowed_grad_years else "Any / Not specified"
    locs = ", ".join(job.locations) if job.locations else "Not specified"
    req_skills = ", ".join(job.required_skills) if job.required_skills else "None specified"
    pref_skills = ", ".join(job.preferred_skills) if job.preferred_skills else "None specified"
    deadline = job.application_deadline.strftime("%Y-%m-%d") if job.application_deadline else "Not specified"
    official_badge = "[OFFICIAL PORTAL]" if job.is_official_portal else "[UNVERIFIED PORTAL]"

    print("\n" + "=" * 70)
    print(f" JOB RECORD #{job.id}: {job.company_name} - {job.role_title}")
    print("=" * 70)
    print(f" - Locations:        {locs}")
    print(f" - Deadline:         {deadline}")
    print(f" - Target Batches:   {batches}")
    print(f" - Eligibility:      {job.eligibility_status.value.upper()}")
    print(f"   Reason:           {job.eligibility_reason}")
    print(f" - Portal Trust:     {official_badge} ({job.verification_status.value})")
    print(f"   Source Notes:     {job.verification_source or 'None'}")
    print(f" - Application Link: {job.application_url or 'None'}")
    print(f" - Required Skills:  {req_skills}")
    print(f" - Preferred Skills: {pref_skills}")
    print(f" - Tracking Status:  {job.application_status.value.upper()}")
    if job.notes:
        print(f" - Profile Match:    {job.notes}")
    print("=" * 70 + "\n")


def print_candidate_profile():
    """Display the current candidate profile and preferences."""
    profile = get_candidate_profile()
    print("\n" + "=" * 70)
    print(f" CANDIDATE PROFILE: {profile.full_name} ({profile.college})")
    print("=" * 70)
    print(f" - Degree:               {profile.degree}")
    print(f" - Current Year:         Year {profile.current_college_year} (Graduation: {profile.graduation_year})")
    print(f" - Target Roles:         {', '.join(profile.target_roles)}")
    print(f" - Current Availability: {profile.current_availability} (Semester)")
    print(f" - Summer Hubs:          {', '.join(profile.summer_locations)}")
    print(f" - Relocation Policy:    {profile.relocation_preference}")
    print(f" - Verified Skills:      {', '.join(profile.verified_skills)}")
    print(f" - Verified Projects:    {len(profile.verified_projects)} recorded")
    for idx, p in enumerate(profile.verified_projects, 1):
        links = []
        if p.github_url:
            links.append(f"GitHub: {p.github_url}")
        if p.live_demo_url:
            links.append(f"Demo: {p.live_demo_url}")
        link_str = f" [{', '.join(links)}]" if links else ""
        print(f"    [{idx}] {p.title} ({', '.join(p.tech_stack)}){link_str}")
        print(f"        \"{p.description}\"")
    print("=" * 70 + "\n")


def cmd_init_db(args):
    """Initialize local SQLite database tables."""
    print("[*] Initializing SQLite database schema...")
    init_db()
    print("[+] Database initialized successfully.")


def cmd_list_jobs(args):
    """Display stored job opportunities in a readable tabular format."""
    status_filter = None
    if args.status:
        try:
            status_filter = ApplicationStatus(args.status.lower())
        except ValueError:
            print(f"[!] Invalid status '{args.status}'. Choices: {[s.value for s in ApplicationStatus]}")
            sys.exit(1)

    with get_session() as session:
        jobs = list_jobs(session, status=status_filter, limit=args.limit)
        if not jobs:
            print("[-] No job postings found.")
            return

        print(f"\n{'ID':<4} | {'Company':<18} | {'Role':<30} | {'Batch':<12} | {'Eligibility':<12} | {'Status':<10}")
        print("-" * 98)
        for j in jobs:
            batches = ",".join(str(y) for y in j.allowed_grad_years) if j.allowed_grad_years else "Any"
            print(
                f"{j.id:<4} | {j.company_name[:18]:<18} | {j.role_title[:30]:<30} | "
                f"{batches[:12]:<12} | {j.eligibility_status.value:<12} | {j.application_status.value:<10}"
            )
        print("-" * 98)
        print(f"Total: {len(jobs)} record(s)\n")


def cmd_show_profile(args):
    """Show current candidate profile details."""
    print_candidate_profile()


def cmd_update_profile(args):
    """Update a specific field in the candidate profile."""
    val = args.value
    try:
        parsed_val = json.loads(val)
    except Exception:
        parsed_val = val

    try:
        update_candidate_profile({args.key: parsed_val})
        print(f"[+] Successfully updated '{args.key}' in candidate profile.")
        print_candidate_profile()
    except KeyError as exc:
        print(f"[!] Error: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"[!] Update failed: {exc}")
        sys.exit(1)


def cmd_add_project(args):
    """Parse text or GitHub repo URL, save to dedicated knowledge base, and sync profile."""
    from src.services.github_service import (
        extract_github_repo_coords,
        ingest_github_project,
        save_project_to_knowledge_base,
    )
    print("[*] Processing project input...")
    try:
        if args.mock:
            print("[*] Running in MOCK mode (simulated LLM project parsing)...")
            project = ProjectItem(
                title="Distributed Task Queue & Job Scheduler",
                tech_stack=["Python", "FastAPI", "Redis", "Docker"],
                description="Engineered an asynchronous task distribution worker pool using Redis queues and Docker containers with exponential backoff retries.",
                github_url="https://github.com/punit/distributed-task-queue",
            )
            folder = save_project_to_knowledge_base(project)
            updated_profile, new_skills = add_project_to_profile(project)
        elif extract_github_repo_coords(args.text):
            print("[*] Detected GitHub repository URL. Fetching README from GitHub...")
            project, new_skills, folder = ingest_github_project(args.text)
        else:
            project = parse_project_from_text(args.text)
            folder = save_project_to_knowledge_base(project)
            updated_profile, new_skills = add_project_to_profile(project)

        print(f"[+] Successfully ingested project: '{project.title}'")
        print(f"[+] Persisted to Knowledge Base: {folder}")
        if new_skills:
            print(f"[+] Auto-detected and added new verified skills: {', '.join(new_skills)}")
        else:
            print("[-] No new skills detected; existing verified skills remain current.")
        print_candidate_profile()

    except Exception as exc:
        print(f"[!] Failed to add project: {exc}")
        sys.exit(1)


def cmd_add_sample(args):
    """Insert a realistic sample job posting to test persistence on disk."""
    sample = JobPosting(
        company_name="Google",
        role_title="Software Engineering Intern (Summer 2026)",
        locations=["Bangalore", "Hyderabad"],
        application_deadline=date(2026, 11, 30),
        application_url="https://careers.google.com/jobs/results/12345",
        allowed_grad_years=[2027, 2028],
        eligibility_status=EligibilityStatus.ELIGIBLE,
        eligibility_reason="Explicitly welcomes 2028 batch students",
        required_skills=["Python", "Data Structures", "Algorithms"],
        preferred_skills=["FastAPI", "Cloud", "Distributed Systems"],
        verification_status=VerificationStatus.VERIFIED_OFFICIAL,
        verification_source="careers.google.com",
        is_official_portal=True,
        application_status=ApplicationStatus.NEW,
    )
    with get_session() as session:
        saved = save_job_posting(session, sample)
        print(f"[+] Sample job inserted with ID: {saved.id} ({saved.company_name} - {saved.role_title})")


def _get_mock_extracted_data(text_or_url: str) -> ExtractedJobData:
    return ExtractedJobData(
        company_name="Amazon",
        role_title="Software Development Engineer Intern (Summer 2026)",
        locations=["Bangalore", "Chennai"],
        application_deadline="2026-11-15",
        application_url="https://amazon.jobs/en/jobs/2500000",
        allowed_grad_years=[2027, 2028],
        is_internship=True,
        required_skills=["Java", "Python", "Data Structures", "Algorithms"],
        preferred_skills=["AWS", "Object Oriented Design"],
        eligibility_notes="Open to 2027 and 2028 graduates pursuing B.Tech CS or related fields.",
        confidence_score=0.96,
    )


def cmd_extract_url(args):
    """Scrape and extract structured job data from a webpage URL."""
    print(f"[*] Processing URL: {args.url}")
    with get_session() as session:
        try:
            if args.mock:
                print("[*] Running in MOCK mode (no live scraping or Gemini API call)...")
                mock_scrape = ScrapeResult(
                    url=args.url,
                    final_url=args.url,
                    success=True,
                    text="Amazon SDE Intern 2026 for 2028 graduates...",
                )
                mock_extract = _get_mock_extracted_data(args.url)
                with patch("src.services.pipeline.scrape_url", return_value=mock_scrape):
                    with patch("src.services.pipeline.extract_job_from_text", return_value=mock_extract):
                        job = process_url_pipeline(session=session, url=args.url, source_type=IngestionSource.CLI_INPUT)
            else:
                job = process_url_pipeline(session=session, url=args.url, source_type=IngestionSource.CLI_INPUT)

            print("[+] Successfully extracted and saved job record!")
            print_job_card(job)
        except Exception as exc:
            print(f"[!] Pipeline Error: {exc}")
            sys.exit(1)


def cmd_extract_text(args):
    """Extract structured job data from raw text or a forwarded message."""
    print("[*] Processing raw text input...")
    with get_session() as session:
        try:
            if args.mock:
                print("[*] Running in MOCK mode (no live Gemini API call)...")
                mock_extract = _get_mock_extracted_data(args.text)
                with patch("src.services.pipeline.extract_job_from_text", return_value=mock_extract):
                    job = process_text_pipeline(session=session, raw_text=args.text, source_type=IngestionSource.CLI_INPUT)
            else:
                job = process_text_pipeline(session=session, raw_text=args.text, source_type=IngestionSource.CLI_INPUT)

            print("[+] Successfully extracted and saved job record!")
            print_job_card(job)
        except Exception as exc:
            print(f"[!] Pipeline Error: {exc}")
            sys.exit(1)


def cmd_extract_image(args):
    """Extract structured job data from a screenshot, flyer, or PDF image."""
    image_path = Path(args.path)
    print(f"[*] Processing media file: {image_path}")
    if not image_path.exists():
        print(f"[!] File not found: {image_path}")
        sys.exit(1)

    with get_session() as session:
        try:
            if args.mock:
                print("[*] Running in MOCK mode (simulated multimodal image extraction)...")
                mock_extract = ExtractedJobData(
                    company_name="Razorpay",
                    role_title="Backend Engineering Intern (Payments)",
                    locations=["Bangalore"],
                    application_deadline="2026-11-20",
                    application_url="https://razorpay.com/jobs/backend-intern",
                    allowed_grad_years=[2027, 2028],
                    is_internship=True,
                    required_skills=["Python", "FastAPI", "SQL", "Distributed Systems"],
                    preferred_skills=["AWS", "Docker"],
                    eligibility_notes="Visual Flyer: Hiring 2027 & 2028 batch CS students for Bangalore office.",
                    confidence_score=0.94,
                )
                with patch("src.services.pipeline.extract_job_from_image", return_value=mock_extract):
                    job = process_image_pipeline(session=session, file_path=image_path, source_type=IngestionSource.CLI_INPUT)
            else:
                job = process_image_pipeline(session=session, file_path=image_path, source_type=IngestionSource.CLI_INPUT)

            print("[+] Successfully extracted and saved job record from image!")
            print_job_card(job)
        except Exception as exc:
            print(f"[!] Pipeline Error: {exc}")
            sys.exit(1)


def cmd_tailor_resume(args):
    """Tailor an ATS-optimized PDF resume for a job posting."""
    from pathlib import Path

    from src.database import get_job_by_id
    from src.services.profile import get_candidate_profile
    from src.services.resume import compute_resume_base_name, rebuild_tailored_resume, tailor_resume_for_job

    job_id = args.job_id
    with get_session() as session:
        job = get_job_by_id(session, job_id)
        if not job:
            print(f"[!] Error: Job record #{job_id} not found in database.")
            sys.exit(1)

        profile = get_candidate_profile()

        rebuild_arg = None
        if getattr(args, "no_fillers", False):
            rebuild_arg = []
        elif getattr(args, "keep_fillers", None):
            rebuild_arg = [int(x) for x in str(args.keep_fillers).split(",") if x.strip().isdigit()]

        try:
            if rebuild_arg is not None:
                payload_path = Path("resumes") / f"{compute_resume_base_name(job)}_payload.json"
                res = rebuild_tailored_resume(profile, job, payload_path, keep_fillers=rebuild_arg)
            else:
                print(f"[*] Tailoring ATS resume for Job #{job_id} ({job.company_name} - {job.role_title})...")
                res = tailor_resume_for_job(profile, job)
            if res.generated_filler_titles:
                print("[FILLER CANDIDATES]")
                for i, title in enumerate(res.generated_filler_titles, start=1):
                    print(f"    {i}. {title}")
                print("    Re-fit page space with: python -m src.interfaces.cli tailor "
                      f"{job_id} --keep-fillers 1,3   (or --no-fillers)")
            print("[+] Successfully generated ATS Resume!")
            print(f"    - Clean ATS PDF:    {res.pdf_file_path}")
            print(f"    - Markdown Copy:    {res.md_file_path}")
            print("\n[ATS KEYWORD AUDIT REPORT]")
            print(f"    - ATS Match Score:  {res.ats_audit.match_percentage}%")
            print(f"    - Matched Keywords: {', '.join(res.ats_audit.matched_keywords) or 'None'}")
            print(f"    - Missing Gaps:     {', '.join(res.ats_audit.missing_keywords) or 'None'}")
            print(f"\n[*] Match Rationale:\n    {res.match_rationale}")
            print(f"\n[*] Tailored LinkedIn Referral Pitch:\n    {res.linkedin_referral_pitch}")
            print(f"\n[*] Cold Email Draft:\n{res.cold_email_draft}")
        except Exception as exc:
            print(f"[!] Resume Tailoring Error: {exc}")
            sys.exit(1)


def cmd_digest(args):
    """Generate and display the daily internship intelligence briefing."""
    from src.services.digest import build_daily_digest, format_digest_text
    with get_session() as session:
        report = build_daily_digest(session)
        print(format_digest_text(report))


def cmd_deadlines(args):
    """List opportunities with deadlines approaching in the next N days."""
    from src.database import get_upcoming_deadlines, today_local
    days = args.days
    today = today_local()
    with get_session() as session:
        upcoming = get_upcoming_deadlines(session, days_ahead=days)
        if not upcoming:
            print(f"[*] No application deadlines found within the next {days} days.")
            return

        print(f"[*] Approaching Deadlines (Next {days} days from {today}):")
        for j in upcoming:
            days_left = (j.application_deadline - today).days
            urgency = "TODAY!" if days_left == 0 else f"{days_left} day(s) left"
            print(f"  - #{j.id} {j.company_name} - {j.role_title} | Due: {j.application_deadline} [{urgency}] (Status: {j.application_status.value})")


def cmd_status(args):
    """Update tracking status for a specific job posting."""
    from src.database import update_job_application_status
    job_id = args.job_id
    new_status = args.status
    notes = args.notes
    with get_session() as session:
        try:
            updated = update_job_application_status(session, job_id, new_status, notes=notes)
            if not updated:
                print(f"[!] Error: Job record #{job_id} not found in database.")
                sys.exit(1)
            print(f"[+] Successfully updated Job #{job_id} ({updated.company_name} - {updated.role_title})")
            print(f"    New Status: {updated.application_status.value.upper()}")
            if updated.notes:
                print(f"    Notes:      {updated.notes}")
        except ValueError as err:
            print(f"[!] Invalid Status: {err}")
            sys.exit(1)


def cmd_run_bot(args):
    """Launch the Telegram Bot long-polling listener."""
    from src.interfaces.bot import run_telegram_bot
    run_telegram_bot()


def cmd_watch(args):
    """Manage the ATS board watchlist for proactive discovery."""
    from src.config import settings
    from src.database import (
        add_watch_target,
        deactivate_watch_target,
        list_watch_targets,
        touch_watch_target,
    )
    from src.services.discovery import fetch_target_listings, is_internship_title

    action = args.action

    if action == "list":
        with get_session() as session:
            targets = list_watch_targets(session, active_only=False)
            if not targets:
                print("[-] Watchlist empty. Add one: python -m src.interfaces.cli watch add lever spotify")
                return
            print(f"\n{'ID':<4} | {'Company':<22} | {'Type':<11} | {'Key':<26} | {'On':<4} | {'Checked':<16}")
            print("-" * 100)
            for t in targets:
                checked = t.last_checked_at.strftime("%Y-%m-%d %H:%M") if t.last_checked_at else "never"
                err = f" [error: {t.last_error[:40]}]" if t.last_error else ""
                print(f"{t.id:<4} | {t.company_name[:22]:<22} | {t.target_type.value.split('_')[0]:<11} | {t.target_key[:26]:<26} | {'yes' if t.active else 'no':<4} | {checked}{err}")
            print("-" * 100)
            print(f"Total: {len(targets)} target(s) | Telegram: /watch list | Manual sweep: /discover\n")
        return

    if action == "add":
        company_label = (args.company or args.key.replace("-", " ")).title().strip()
        if args.type in {"url", "custom"} and not str(args.key).startswith(("http://", "https://")):
            key = "https://" + str(args.key).lstrip("/")
        else:
            key = args.key
        with get_session() as session:
            try:
                target = add_watch_target(session, company_label, args.type, key)
            except ValueError as exc:
                print(f"[!] {exc}")
                print("    Allowed types: ashby, greenhouse, lever, url")
                sys.exit(1)
            print(f"[+] Watch target #{target.id} registered: {target.company_name} ({target.target_type.value})")
            print(f"    Poll interval: every {settings.DISCOVERY_CHECK_INTERVAL_HOURS}h | Max ingested/run: {settings.DISCOVERY_MAX_PER_RUN}")

            print("[*] Verifying board is reachable...")
            try:
                listings = fetch_target_listings(target)
                intern_count = sum(1 for l in listings if is_internship_title(l.title))
                print(f"[+] Board reachable. Live postings: {len(listings)} (approx {intern_count} intern/trainee roles)")
            except Exception as exc:
                touch_watch_target(session, target.id, error=str(exc)[:400])
                print(f"[!] Board check FAILED: {exc}")
                print("    Double-check the slug/token in a browser; discovery will keep retrying.")
        return

    if action == "remove":
        with get_session() as session:
            target = deactivate_watch_target(session, args.target_id)
            if target:
                print(f"[+] Stopped watching '{target.company_name}' (#{target.id}).")
            else:
                print(f"[!] Watch target #{args.target_id} not found.")
                sys.exit(1)


def cmd_discover(args):
    """Run one discovery sweep across all active watch targets."""
    from src.services.discovery import is_alertable, run_discovery

    with get_session() as session:
        outcomes = run_discovery(session)

    if not outcomes:
        print("[-] No new postings discovered across your watchlist.")
        return

    for outcome in outcomes:
        tag = "[ALERT]" if is_alertable(outcome) else "[quiet]"
        print(f"{tag} #{outcome.job.id} {outcome.job.company_name} - {outcome.job.role_title} "
              f"(score {int(outcome.match_score * 100)}%, {outcome.job.verification_status.value})")
        print_job_card(outcome.job)

    print(f"[+] Ingested {len(outcomes)} new posting(s). Telegram pushes will match the [ALERT] filter.")


def main():
    parser = argparse.ArgumentParser(description="AI Internship Intelligence CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # init-db
    subparsers.add_parser("init-db", help="Create database tables")

    # list-jobs
    list_parser = subparsers.add_parser("list-jobs", help="List stored job postings")
    list_parser.add_argument("--status", type=str, help="Filter by status (new, applied, etc.)")
    list_parser.add_argument("--limit", type=int, default=20, help="Max records to return")

    # profile commands
    subparsers.add_parser("show-profile", help="Display candidate profile and preferences")
    
    update_prof = subparsers.add_parser("update-profile", help="Update a candidate profile field")
    update_prof.add_argument("--key", type=str, required=True, help="Field to update (e.g. graduation_year, summer_locations)")
    update_prof.add_argument("--value", type=str, required=True, help="New value (string or JSON array/object)")

    # add-project command
    proj_parser = subparsers.add_parser("add-project", help="Intelligently parse and add a project to your profile")
    proj_parser.add_argument("text", type=str, help="Raw project description or GitHub README snippet")
    proj_parser.add_argument("--mock", action="store_true", help="Run with mock data (offline, no API key needed)")

    # extract-url
    url_parser = subparsers.add_parser("extract-url", help="Scrape & extract job details from a URL")
    url_parser.add_argument("url", type=str, help="Job posting URL")
    url_parser.add_argument("--mock", action="store_true", help="Run with mock data (offline, no API key needed)")

    # extract-text
    text_parser = subparsers.add_parser("extract-text", help="Extract job details from raw text")
    text_parser.add_argument("text", type=str, help="Job description text")
    text_parser.add_argument("--mock", action="store_true", help="Run with mock data (offline, no API key needed)")

    # extract-image
    img_parser = subparsers.add_parser("extract-image", help="Extract job details from a screenshot, photo, or PDF")
    img_parser.add_argument("path", type=str, help="Path to image or PDF file")
    img_parser.add_argument("--mock", action="store_true", help="Run with mock data (offline, no API key needed)")

    # tailor
    tailor_parser = subparsers.add_parser("tailor", help="Tailor an ATS-optimized PDF resume for a job")
    tailor_parser.add_argument("job_id", type=int, help="Target job record ID from database")
    tailor_parser.add_argument("--keep-fillers", type=str, default=None, metavar="1,3",
                               help="Re-fit from cached payload keeping filler ranks 1-3")
    tailor_parser.add_argument("--no-fillers", action="store_true",
                               help="Re-fit from cached payload with all filler projects dropped")

    # run-bot
    subparsers.add_parser("run-bot", help="Start the Telegram Bot long-polling daemon")

    # watch (discovery watchlist management)
    watch_parser = subparsers.add_parser("watch", help="Manage ATS board watchlist for proactive discovery")
    watch_sub = watch_parser.add_subparsers(dest="action", required=True)
    watch_add = watch_sub.add_parser("add", help="Watch an ATS board or careers URL")
    watch_add.add_argument("type", type=str, help="Board type: ashby | greenhouse | lever | url")
    watch_add.add_argument("key", type=str, help="Board slug/token, or careers page URL for 'url' type")
    watch_add.add_argument("--company", type=str, default=None, help="Company label (default: prettified key)")
    watch_sub.add_parser("list", help="List watch targets and their status")
    watch_remove = watch_sub.add_parser("remove", help="Stop watching a target")
    watch_remove.add_argument("target_id", type=int, help="Watch target ID from 'watch list'")

    # discover (manual discovery sweep)
    subparsers.add_parser("discover", help="Run one discovery sweep across active watch targets")

    # digest
    subparsers.add_parser("digest", help="Display daily internship intelligence briefing")

    # deadlines
    dl_parser = subparsers.add_parser("deadlines", help="View approaching application deadlines")
    dl_parser.add_argument("--days", type=int, default=7, help="Days ahead to search (default: 7)")

    # status
    status_parser = subparsers.add_parser("status", help="Update job application status")
    status_parser.add_argument("job_id", type=int, help="Target job record ID")
    status_parser.add_argument("status", type=str, help="New status: new, applied, interviewing, offer, rejected, archived")
    status_parser.add_argument("--notes", type=str, default=None, help="Optional tracking notes")

    args = parser.parse_args()
    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "list-jobs":
        cmd_list_jobs(args)
    elif args.command == "show-profile":
        cmd_show_profile(args)
    elif args.command == "update-profile":
        cmd_update_profile(args)
    elif args.command == "add-project":
        cmd_add_project(args)
    elif args.command == "extract-url":
        cmd_extract_url(args)
    elif args.command == "extract-text":
        cmd_extract_text(args)
    elif args.command == "extract-image":
        cmd_extract_image(args)
    elif args.command == "tailor":
        cmd_tailor_resume(args)
    elif args.command == "run-bot":
        cmd_run_bot(args)
    elif args.command == "watch":
        cmd_watch(args)
    elif args.command == "discover":
        cmd_discover(args)
    elif args.command == "digest":
        cmd_digest(args)
    elif args.command == "deadlines":
        cmd_deadlines(args)
    elif args.command == "status":
        cmd_status(args)


if __name__ == "__main__":
    main()
