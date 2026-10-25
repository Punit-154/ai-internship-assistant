# ARCHITECTURE.md — Project Guide & Debugging Manual

> **Living document.** Whenever a feature is added, changed, or removed, update the relevant
> section here and add an entry to the Change Log (Section 12). If code and this doc disagree,
> one of them is wrong — fix it immediately.
>
> **Version baseline:** v1.0 "Working MVP" (commit `4ab5b9b`) · Last updated: 2026-10-21

---

## 1. What This Project Is (30-Second Pitch)

A **single-user, fully-local internship intelligence system** for a VIT Chennai CSE student
(2028 batch). You feed it internship postings as **links, raw text, or screenshots** — it
scrapes/extracts them with Gemini, **verifies they are real** (anti-scam + official-portal
checks), **scores them against your verified profile**, tracks your application lifecycle,
and generates **truthful, ATS-parseable, single-page PDF resumes** tailored per job using
your real GitHub project READMEs. Delivered through **Telegram** (mobile-first) and a **CLI**.

**Design philosophy — "Zero Fabrication":** the system will never invent a skill, project,
or metric. Anything on a generated resume must trace back to `data/candidate_profile.json`
or the GitHub knowledge base in `data/projects/`.

**Key talking points (for interviews):**
- Layered architecture: `models → services → interfaces`, no circular imports
- ATS API resolvers (Ashby/Greenhouse/Lever) hit **official JSON APIs** instead of scraping HTML
- Every raw input is audit-logged to `raw_ingestions` **before** processing — nothing is lost on failure
- LLM output is **schema-enforced** via Pydantic (`response_schema`), not free-text parsing
- Model fallback chain survives Gemini 429/503 quota errors
- 68 pytest tests with an isolated in-memory DB

---

## 2. Mental Model — How Data Flows

```
                        ┌──────────── INGESTION (3 doors) ────────────┐
                        │                                             │
             Telegram URL / CLI        Telegram text / CLI        Telegram photo / PDF / CLI
                        │                        │                        │
                        ▼                        ▼                        ▼
              process_url_pipeline     process_text_pipeline    process_image_pipeline
              (src/services/pipeline.py)      │                        │
                        │                     │            Gemini Vision (multimodal)
                        ▼                     ▼                        ▼
                 scrape_url()          extract_job_from_text()  extract_job_from_image()
                 (scraper.py)          (extractor.py)           (extractor.py)
                 ├─ Ashby API          └─ Pydantic: ExtractedJobData
                 ├─ Greenhouse API
                 ├─ Lever API          ┌──────────────────────────────┐
                 ├─ httpx+trafilatura  │ enrich_job_with_live_search()│  (text & image only)
                 └─ DuckDuckGo fallback│  finds missing URL, re-      │
                        │              │  scrapes to fill gaps        │
                        ▼              └──────────────────────────────┘
                 ExtractedJobData ──────────────┐
                                        ▼
                              verify_opportunity()          evaluate_job_match()
                              (verifier.py)                 (profile.py)
                              scam regex + ATS whitelist    batch 40% / role 30% /
                              + DDGS portal discovery       location 15% / skills 15%
                                        │                          │
                                        ▼                          ▼
                              save_or_update_job_posting()  (database.py — dedup merge)
                                        │
                                        ▼
                                  SQLite (internships.db)
                                        │
              ┌─────────────────────────┼──────────────────────────┐
              ▼                         ▼                          ▼
      /tailor <id>                 /status <id>               /digest  /deadlines
      resume.py → pdf_generator.py  update_job_application_   digest.py (build_daily_digest)
      → resumes/*.pdf + *.md        status()                  → Telegram HTML / CLI text
      → LinkedIn pitch + cold email
```

### The three pipelines, step by step

**A. URL pipeline** — `process_url_pipeline(session, url)` in `src/services/pipeline.py:100`
1. Log raw URL to `raw_ingestions`
2. `scrape_url()` → tries ATS APIs → Workday straight to headless Chromium → httpx → Chromium retry on thin shell / 403 → DuckDuckGo fallback
3. `extract_job_from_text()` → structured `ExtractedJobData`
4. `verify_opportunity()` → trust verdict; if it finds an official portal URL, that **overrides** the scraped URL
5. `evaluate_job_match()` → score + skill match/gap lists
6. Build `JobPosting` (notes = red flags + match breakdown), save with dedup-merge
7. Mark raw record `processed=True`

**B. Text pipeline** — `process_text_pipeline()` at `pipeline.py:186`
Same as A, but skips scraping first; instead runs `enrich_job_with_live_search()`
(`pipeline.py:268`): if no URL → DDGS-discover one; if data has gaps (<3 skills, no batch,
no deadline, no location) → scrape the URL and merge the missing fields.

**C. Image pipeline** — `process_image_pipeline()` at `pipeline.py:336`
Validates MIME (png/jpeg/webp/pdf) → copies file to `data/uploads/` with timestamped name →
logs raw record → `extract_job_from_image()` (Gemini vision) → then identical enrich/verify/
match/save steps as B.

---

## 3. Repository Map

| Path | Role | You touch it when… |
|---|---|---|
| `src/config.py` | Pydantic Settings singleton (`settings`), loads `.env`, creates `data/uploads/` | Adding any new env var / API key |
| `src/database.py` | Engine, sessions, all DB queries (dedup, merge, deadlines, status, scheduler ledger, watch targets) | Adding tables, changing queries |
| `src/models/job.py` | `JobPosting` + `RawIngestion` SQLModel tables + all 4 enums (+ `IngestionSource.DISCOVERY`) | Adding a column to jobs |
| `src/models/system.py` | `SchedulerLog` (idempotency ledger) + `WatchTarget` (board watchlist) tables + their enums | Changing scheduler/dedup or watchlist schema |
| `src/models/candidate.py` | `CandidateProfile`, `ProjectItem` — **Pydantic only, not DB tables** | Adding profile fields |
| `src/services/pipeline.py` | Orchestrator: the 3 ingestion pipelines + gap-filling | Changing ingestion behavior |
| `src/services/scraper.py` | HTTPX fetch, trafilatura extraction, 3 ATS API resolvers, optional Playwright renderer (JS-only ATS e.g. Workday), DDGS fallback | New ATS, fetch bugs |
| `src/services/extractor.py` | Gemini text/vision extraction into `ExtractedJobData`, model fallback chain | Changing extraction schema/prompt |
| `src/services/verifier.py` | Scam regexes, trusted ATS whitelist, DDGS portal discovery | Trust/scam logic |
| `src/services/profile.py` | Profile load/save/update, project parsing, `evaluate_job_match` scoring | Match logic, profile fields |
| `src/services/github_service.py` | GitHub README fetch, KB persistence (`data/projects/`), profile sync | Project ingestion |
| `src/services/resume.py` | ATS keyword extraction + audit, tailoring prompt, markdown render | Resume content/audit |
| `src/services/pdf_generator.py` | ReportLab vector PDF builder (all layout code) | Resume visual layout |
| `src/services/digest.py` | Daily digest builder + HTML/text formatters + deadline-reminder HTML | Digest content |
| `src/services/skills.py` | Skill alias/acronym normalizer ("DRF"↔"Django REST Framework"); used by profile + resume | Adding alias pairs |
| `src/services/discovery.py` | Proactive ATS board sweeper: `run_discovery`, fetchers (Ashby/Greenhouse/Lever/custom URL), intake filter, alerting | New board type, sweep logic |
| `src/interfaces/cli.py` | 15 argparse subcommands, mock mode, terminal cards | New CLI command |
| `src/interfaces/bot.py` | All Telegram handlers, inline keyboards, HTML cards, scheduler callbacks (`schedule_background_jobs`) | New bot command |
| `data/candidate_profile.json` | **The single source of truth** for your profile/skills/projects | Never edit by hand blindly — use CLI |
| `data/projects/<slug>/` | Project KB: `README.md` + `project.json` per project | Auto-managed by github_service |
| `data/uploads/` | Downloaded Telegram media (auto-created) | Debugging image ingestion |
| `resumes/` | Generated `Resume_*_Job{id}.pdf` + `.md` | Output only |
| `tests/` | 17 files, 111 tests, in-memory SQLite via `conftest.py` | Every feature addition |
| `internships.db` | SQLite database (git-ignored state) | Inspect with sqlite3 |

**Dead code to know about (don't get confused while debugging):**
- `pipeline.verify_portal_authenticity()` and `pipeline.evaluate_batch_eligibility()` — defined
  but unused; pipelines call `verify_opportunity` / `evaluate_job_match` directly.
- `cli.cmd_add_sample()` — defined, never registered in the argparse parser.
- `bot.py:339` comment mentions ".tex file" — stale; LaTeX was replaced by ReportLab.

---

## 4. Data Models & Storage

### SQLite tables (`internships.db`)

**`job_postings`** — one row per unique (company, role) opportunity:

| Field | Type | Notes |
|---|---|---|
| `id` | PK | Referenced by every command (`/tailor 8`) |
| `created_at` / `updated_at` | datetime | UTC, auto-set |
| `raw_ingestion_id` | FK → raw_ingestions | Traceability to the original input |
| `company_name`, `role_title` | str, indexed | The dedup key pair |
| `locations` | JSON list | e.g. `["Bangalore", "Remote"]` |
| `application_deadline` | date (nullable) | **Only parses `YYYY-MM-DD`** — see Debugging D7 |
| `application_url` | str (nullable) | Overridden by verifier's official URL when found |
| `allowed_grad_years` | JSON list of int | Drives eligibility |
| `eligibility_status` | enum | `eligible` / `ineligible` / `uncertain` |
| `eligibility_reason` | str | Human-readable why |
| `required_skills`, `preferred_skills` | JSON lists | Fuel the ATS audit |
| `verification_status` | enum | `verified_official` / `unverified_third_party` / `expired_or_dead` / `suspicious` |
| `verification_source`, `verification_notes` | str | Why it was trusted/flagged |
| `is_official_portal` | bool | Quick-check flag |
| `application_status` | enum, indexed | Lifecycle: `new → reviewed → applied → interviewing → offer / rejected / archived` |
| `notes` | str | Match breakdown + red flags appended by pipelines |

**`raw_ingestions`** — append-only audit log. `source_type` ∈ {`telegram_url`, `telegram_image`,
`telegram_text`, `cli_input`, `manual_entry`, `discovery`}; `processed` bool; `error_message` on failure.
**This table is your first debugging stop** (Section 7).

**`scheduler_log`** (v1.1) — idempotency ledger so background jobs never double-send:
`(event_type, event_key, sent_date)` is the dedup key. `event_type` ∈
{`daily_digest`, `deadline_reminder`, `discovery_run`}; `event_key` = `"digest"` for the digest
or the job id for reminders. Helper: `database.has_scheduler_event` / `log_scheduler_event`.

**`watch_targets`** (v1.1) — the discovery watchlist: `company_name`, `target_type`
(`ashby_board` / `greenhouse_board` / `lever_board` / `custom_url`), `target_key` (slug/token
or URL), `active`, `last_checked_at`, `last_error`, `notes`. CRUD helpers live in
`database.py` (`add_watch_target` accepts shorthand types: ashby/greenhouse/gh/lever/url/custom).

### JSON storage (not in DB)

**`data/candidate_profile.json`** — `CandidateProfile`: identity, academics (CGPA,
grad 2028), contact links, `target_roles`, availability (`Remote` during semester),
`summer_locations` (Bangalore/Chennai/Hyderabad), `verified_skills` (~50 entries),
`verified_projects` (list of `ProjectItem`: title, tech_stack, description, github_url,
date_range, bullet_points), `research_experience`, `achievements`.

**`data/projects/<slug>/`** — per-project folder with the full raw `README.md` + structured
`project.json`. The resume tailoring prompt injects the first 1200 chars of each README.

---

## 5. Service Modules In Depth

### 5.1 `scraper.py` — fetch anything
`scrape_url(url, timeout=12) → ScrapeResult(url, final_url, success, text, status_code, error_message)`

Resolution order (first win returns):
1. **`_resolve_ashby_job`** — parses `ashby_jid` (query param or URL path), guesses board slug
   from domain, hits `api.ashbyhq.com/posting-api/job-board/{slug}`
2. **`_resolve_greenhouse_job`** — `gh_jid` + board token → `boards-api.greenhouse.io/v1/boards/...`
3. **`_resolve_lever_job`** — `lever_jid` + slug → `api.lever.co/v0/postings/...`
4. **JS-only portals** — `myworkdayjobs.com` (Workday) hosts are known to answer static HTTP
   with an empty shell, so they go straight to `_playwright_fetch` when available
5. **httpx GET** with Chrome-like headers → `_extract_clean_text()` (`trafilatura.extract()`
   with `trafilatura.baseline` fallback, discarded under the 50-char threshold)
6. **`_playwright_fetch` retry** — on thin SPA shells (HTTP <400, tiny text) or bot-challenge
   statuses (401/403/429/503): headless Chromium renders the page (images/media/fonts blocked),
   settles via a short `networkidle` window **plus** explicit waits for hydration-dependent
   job-content selectors (`data-automation-id="jobDescription…"`, `main`, `[role="main"]` …
   — `networkidle` alone is insufficient on angular Workday apps), same 50-char quality gate as HTTP
7. **`_search_grounding_fallback`** — on anything still failing (incl. HTTP ≥400, timeout,
   network error): builds a DDGS query from URL segments, returns combined snippets as
   synthetic "text"

Browser availability: `playwright_available()` = package importable **and**
`settings.PLAYWRIGHT_ENABLED` (default True, toggle via `.env`). Browsers launch per call from
worker threads (safe with Python Telegram Bot's `asyncio.to_thread` wrappers); nothing is
cached across URLs at the moment.

### 5.2 `extractor.py` — LLM with a contract
- Schema: `ExtractedJobData` (company, role, locations, deadline `YYYY-MM-DD` string, URL,
  grad years, `is_internship`, required/preferred skills, eligibility_notes, confidence 0–1)
- System prompt hard-rules: no invention; grad-year mapping (1st yr→2029, 2nd→2028,
  3rd→2027, 4th→2026, reference year 2026); non-internship detection
- `generate_content_with_fallback()`: tries primary model, on transient errors
  (429/503/500/404/quota/"high demand") sleeps 1s and walks `FALLBACK_MODELS`
- Client validation rejects placeholder keys starting `AIzaSy_your_gemini`
- Text needs ≥15 chars; image bytes ≥20
- Batch helpers (v1.1): `extract_multiple_jobs_from_text(blocks)` returns
  `List[Optional[ExtractedJobData]]` (None slot per failed block), and
  `extract_jobs_list_from_text(page_text, max_jobs=10)` parses whole careers/listing pages
  into `ExtractedJobBundle`/`ExtractedJobsList` JSON — used by `discovery.py`, never raises

### 5.3 `verifier.py` — trust engine
- `detect_scam_signals()`: 4 regex families (fees/deposits, money transfers, crypto-task scams,
  WhatsApp/Telegram-only hiring) + free-email-domain check + `t.me` URL check
- `TRUSTED_ATS_DOMAINS`: 18 domains (greenhouse.io, lever.co, myworkdayjobs.com, ashbyhq.com,
  amazon.jobs, careers.google.com, …)
- `search_official_career_portal()`: 2 DDGS queries; accepts trusted-ATS hit → company-name-in-
  hostname hit → reputable-platform fallback (LinkedIn jobs, Wellfound, Unstop, Internshala)
- `verify_opportunity()` decision order: **scam flags → SUSPICIOUS** (hard stop) → URL on
  trusted ATS → company name in hostname → Google-Form link triggers grounding → no URL/
  unverified triggers grounding → else `UNVERIFIED_THIRD_PARTY`

### 5.4 `profile.py` — who you are + how well a job fits
- `get_candidate_profile()`: loads JSON; **creates defaults if missing; silently returns
  defaults if the JSON is corrupt** (debugging gotcha D9)
- `add_project_to_profile()`: dedups by github_url or exact title (replaces in place);
  auto-appends unseen `tech_stack` entries into `verified_skills`
- `evaluate_job_match()` → `MatchEvaluation`. Score composition:
  - **+0.40** batch eligible / **+0.25** batch uncertain / 0 ineligible
  - **+0.30** role title contains any keyword (software, backend, cloud, ai, ml, data, engineer, intern, …)
  - **+0.15** location viable (remote, or in summer hubs, or anything — everything is "viable")
  - **+0.15 × skill ratio** — matched via `skills.find_matching_skill()` (alias-aware since v1.1:
    "DRF"↔"Django REST Framework", "K8s"↔Kubernetes, Go↔Golang, REST↔RESTful APIs, …with
    anti-containment pairs so C≠C++ and Git≠GitHub)

### 5.5 `github_service.py` — the knowledge base
`ingest_github_project(url)` → regex-extract owner/repo → fetch raw README (main, then master)
→ Gemini `README_ANALYZER` prompt → `ProjectItem` → `save_project_to_knowledge_base()`
(writes `README.md` + `project.json` under `data/projects/<slug>/`) → `add_project_to_profile()`
→ returns `(project, new_skills, folder_path)`. `get_all_stored_projects()` reads them all back.

### 5.6 `resume.py` + `pdf_generator.py` — the output engine
`tailor_resume_for_job(profile, job)`:
1. Load all stored projects (README snippets ≤1200 chars) as LLM context
2. `extract_ats_keywords_from_job()`: required+preferred skills + a hardcoded `common_terms`
   scan over `role_title + notes`
3. `audit_ats_keywords()`: substring match against profile skills + project tech stacks →
   `ATSAuditReport(matched, missing, match_percentage)`
4. One big Gemini call → `ResumeTailorLLMResponse` (categorized skills, ≤3 tailored projects
   with X-Y-Z bullets, LinkedIn pitch <100 words, cold email <120 words) — prompt repeats the
   zero-fabrication constraint and explicitly lists missing keywords as DO-NOT-INVENT
5. GitHub URLs re-attached by fuzzy title matching
6. Markdown companion written; `generate_resume_pdf()` renders ReportLab vector PDF:
   Helvetica only, letter size, 32pt side margins, centered clickable header, uppercase
   section titles + HR rules, 2-col date tables, categorized skills, dynamic bullet depth
   (2 bullets for first 2 projects, 1 for the rest) to force single-page fit
7. Files: `resumes/Resume_{Company}_{Role}_Job{id}.pdf` + `.md`
- v1.1: keyword extraction & audit are alias-aware via `skills.py` (`find_matching_skill`),
  so "Python (DRF)" postings match your "Django" profile skill

### 5.7 `digest.py` — the daily brain
`build_daily_digest(session)`: scores all NEW/REVIEWED jobs (threshold **0.65** or explicitly
ELIGIBLE) → top 5; deadlines within 7 days; active applications (APPLIED/INTERVIEWING/OFFER);
expired deadlines (still NEW/REVIEWED with past date). Generates prioritized action items
(urgent ≤3 days first). Three formatters: `format_digest_html` (Telegram) /
`format_digest_text` (CLI) / `format_deadline_reminder_html` (v1.1 scheduler reminders,
grouped by urgency 🔥 ≤1d / ⚠️ ≤2d).

### 5.8 `skills.py` — alias resolution (v1.1)
`find_matching_skill(text_a, text_b)` → True if the two strings describe the same skill:
1. Normalize: lowercase, strip punctuation, collapse internships/of/confusion words
2. Canonicalize via `_SKILL_ALIASES` (≈40 acronym/alias groups: DRF→django rest framework,
   K8s→kubernetes, LLM→large language models, AWS→amazon web services, REST→restful apis,
   MERN→mongodb express react nodejs, Golang→Go, PyTorch→torch, …)
3. Match = equality OR either-canonical is contained in the other, **minus anti-containment
   pairs** (`C` vs `C++`/`C#`, `Java` vs `JavaScript`, `Git` vs `GitHub`) so short skills
   can't falsely match longer ones
4. `skill_group()` returns an optional category label (used in display only). To teach the
   system a new alias, edit `_SKILL_ALIASES` in place.

### 5.9 `discovery.py` — the proactive sweeper (v1.1)
Core: `run_discovery(session, ref_date, llm_client, max_per_run)` — iterates every **active**
row in `watch_targets`, fetches its live board JSON, ingests unseen intern/trainee postings,
scores them, and saves them with `IngestionSource.DISCOVERY`. Per-target errors are isolated
and recorded via `database.touch_watch_target` (bad target ≠ dead sweep).

- **Fetchers** (plain `httpx` JSON, no scraping): `fetch_ashby_board` (Posting API,
  respects `isListed`, `descriptionPlain` included), `fetch_greenhouse_board` + per-job
  `fetch_greenhouse_job_content` (list endpoint has NO descriptions — descriptions need a
  second GET `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}`), `fetch_lever_board`
  (`descriptionPlain` first-class), and `fetch_custom_url` (any careers URL → re-uses
  `scraper.scrape_url`, each `<a>` whose href/title looks like a posting becomes a
  `DiscoveredListing`; also trafilatura-fallbacks to full-page text). Dispatch entry:
  `fetch_target_listings(target)`.
- **Intake filter** `is_internship_title`: requires an internship-flavored keyword
  (intern/internship/trainee/co-op/student/graduate/campus/apprentice) and excludes
  senior/staff/principal/lead/manager/director/VP/PhD/postdoc/experienced.
- **Dedup**: post exists if a `JobPosting` with the same company + role already has one of
  the URLs → skipped (new URL on an existing pair counts as seen).
- **LLM step (single batched call per target)**: postings' description text is sent as
  numbered blocks through `extractor.extract_multiple_jobs_from_text`; failures leave the
  block's slot None and that posting is skipped gracefully (source raw still recorded).
- **Verification & scoring**: the freshly built `JobPosting` (with ATS-canonical URL) runs
  `verifier.verify_opportunity` (scam scan first) + `profile.evaluate_job_match`.
- **Alerting** `is_alertable(outcome)`: not SUSPICIOUS, not INELIGIBLE, and
  `match_score >= settings.DISCOVERY_ALERT_MIN_SCORE (0.65)`. Alert-worthy → Telegram push;
  the rest are saved silently (still visible in /list).
- **Scheduler glue**: `run_discovery` stamps `SchedulerLog(event_type=discovery_run)` for
  the (target, date) so manual/automated sweeps double-check each other.

---

## 6. Interfaces

### CLI → internal call map (`python -m src.interfaces.cli <cmd>`)

| Command | Calls | Notes |
|---|---|---|
| `init-db` | `database.init_db()` | Safe to re-run |
| `list-jobs [--status] [--limit]` | `database.list_jobs()` | Table view |
| `show-profile` | `profile.get_candidate_profile()` | Read-only |
| `update-profile --key K --value V` | `profile.update_candidate_profile()` | V parsed as JSON if possible |
| `add-project <text\|url> [--mock]` | `github_service.ingest_github_project()` or `profile.parse_project_from_text()` | Mock = fake project, no API |
| `extract-url <url> [--mock]` | `pipeline.process_url_pipeline()` | Mock patches scraper+extractor |
| `extract-text <text> [--mock]` | `pipeline.process_text_pipeline()` | Mock patches extractor |
| `extract-image <path> [--mock]` | `pipeline.process_image_pipeline()` | Mock patches vision call |
| `tailor <job_id>` | `resume.tailor_resume_for_job()` | Prints audit + pitch + email |
| `digest` | `digest.build_daily_digest()` | Text format |
| `deadlines [--days N]` | `database.get_upcoming_deadlines()` | Default 7 |
| `status <id> <status> [--notes]` | `database.update_job_application_status()` | Validates enum |
| `run-bot` | `bot.run_telegram_bot()` | Long-polling daemon + background scheduler |
| `watch add <type> <key> [--company]` | `database.add_watch_target()` + reachability probe | type: ashby/greenhouse/gh/lever/url/custom |
| `watch list` | `database.list_watch_targets()` | Shows last-checked time + last error |
| `watch remove <target_id>` | `database.deactivate_watch_target()` | Soft-deactivates |
| `discover` | `discovery.run_discovery()` | One sweep now; prints [ALERT]/[quiet] per posting |

### Telegram message → handler map

| You send | Handler | Result |
|---|---|---|
| `/start`, `/help` | `cmd_start` | Welcome + command cheat sheet |
| `/profile` | `cmd_profile` | Profile summary HTML |
| `/addproject <url\|text>` | `cmd_add_project` | GitHub ingest or text-parse → KB + profile sync |
| `/list` | `cmd_list` | Last 10 jobs |
| `/tailor <id>` | `cmd_tailor` | Summary + PDF as document |
| `/status <id> <status> [notes]` | `cmd_status` | Updated card + keyboard |
| `/deadlines [days]` | `cmd_deadlines` | Urgency-badged list (default 14 days) |
| `/digest` | `cmd_digest` | On-demand briefing |
| `/watch add\|list\|remove` | `cmd_watch` | Same semantics as CLI (board is probed before saving) |
| `/discover` | `cmd_discover` | Manual sweep; pushes alert-worthy postings as cards |
| Any message with a URL | `handle_text_message` | URL pipeline → card + inline keyboard |
| Any other text | `handle_text_message` | Text pipeline → card |
| Photo | `handle_photo_message` | Downloads largest size → image pipeline |
| Document (PDF etc.) | `handle_document_message` | Downloads → image pipeline |
| Button tap | `handle_callback_query` | `status:{id}:{status}` or `tailor:{id}` |

**Authorization:** every handler checks `is_authorized(user_id)` against
`TELEGRAM_ALLOWED_USER_ID`. ⚠️ If that env var is unset, **anyone** can use your bot.

**Background schedule (v1.1, `schedule_background_jobs` in `build_telegram_app`):**
- `run_daily` → `scheduled_daily_digest` at `DIGEST_HOUR_LOCAL` (default 08:00 IST)
- `run_repeating` → `scheduled_deadline_check` every `DEADLINE_CHECK_INTERVAL_HOURS` (first run +180s)
- `run_repeating` → `scheduled_discovery_check` every `DISCOVERY_CHECK_INTERVAL_HOURS` (first run +90s)
- All three wrap blocking work in `asyncio.to_thread`, dedupe through `scheduler_log`, and log
  failures with `logger.error` (a failing job never kills the scheduler). Requires the
  `job-queue` extra (installs APScheduler) or jobs are skipped with a warning.

---

## 7. Debugging Playbook — "When X Breaks"

### Golden rules
1. **Check `raw_ingestions` first.** Every input is logged before processing. If `processed=0`
   with an `error_message`, the failure point is recorded there.
2. **Reproduce without Telegram.** Almost every bot failure is reproducible via the CLI
   (`extract-url`, `extract-text`, `extract-image`) — same pipeline, easier to iterate.
3. **Use `--mock` flags** to bisect: mock mode patches the scraper/LLM calls, so if the mock
   run succeeds but the live run fails, the problem is network or Gemini, not your data logic.
4. **Run the tests** after any change: `pytest -v` (uses in-memory SQLite — cannot touch your real DB).

### Inspect the database

```bash
# All jobs, newest first
sqlite3 internships.db "SELECT id, company_name, role_title, eligibility_status, verification_status, application_status FROM job_postings ORDER BY id DESC;"

# Failed/unprocessed ingestions — THE debugging table
sqlite3 internships.db "SELECT id, source_type, processed, error_message, substr(raw_content,1,80) FROM raw_ingestions ORDER BY id DESC LIMIT 10;"

# Purge duplicates (utility that exists in code)
python -c "from src.database import get_session, purge_duplicate_jobs; s=next(get_session()); print(purge_duplicate_jobs(s))"
```

### Symptom → cause → fix

**D1. `Web scraping failed: HTTP 403 ... Cloudflare`**
- *Where:* `scraper.scrape_url` → all strategies failed.
- *Why:* WAF/bot challenge; trafilatura got empty SPA shell; DDGS fallback also found nothing.
- *Fix:* Open the URL in a browser; if it's a JS-heavy portal, paste the text via
  `extract-text` instead. Long-term: add a resolver (see Section 10 — Workday).

**D2. `LLM Extraction failed` / empty response / `GEMINI_API_KEY is missing or invalid`**
- *Where:* `extractor.get_genai_client` (key check) or the extraction call.
- *Why:* Missing/placeholder key; quota exhausted across ALL fallback models; input <15 chars.
- *Fix:* Check `.env`; the fallback chain already retries 429s with 1s pauses — if all models
  fail, you're hard rate-limited; wait or reduce call volume.

**D3. Bot never replies at all**
- *Where:* `bot.run_telegram_bot` / `is_authorized`.
- *Why:* (a) `TELEGRAM_ALLOWED_USER_ID` set to a different account — every handler silently
  returns; (b) token placeholder → ValueError at startup; (c) process died — check the
  PM2/service logs; `error_handler` logs TimedOut/NetworkError as warnings and keeps polling.
- *Fix:* Verify user ID via @userinfobot; restart the daemon.

**D4. Bot replies "Processing…" but the message never updates to a card**
- *Where:* Exception mid-pipeline inside `handle_*_message`.
- *Why:* Any pipeline error after the initial `reply_text`; the `except` branch should edit
  the message with the error — if even that failed (network), it's silent.
- *Fix:* Check daemon logs + `raw_ingestions.error_message`.

**D5. Sent the same link twice — no new job ID appeared**
- *Not a bug.* `save_or_update_job_posting` dedups on normalized (company, role) and **merges**
  missing fields into the existing row. The returned card shows the OLD id with enriched data.

**D6. Eligibility verdict is wrong (says INELIGIBLE / uncertain)**
- *Where:* extractor prompt grad-year mapping → `allowed_grad_years` → `evaluate_job_match` batch rule.
- *Why:* The LLM mapped "pre-final year" per its rules (3rd→2027), or the posting targets
  2027 only, or the posting stated no batch (→ `uncertain`, +0.25 not +0.40).
- *Fix:* Inspect `allowed_grad_years` in the DB row; re-ingest with clearer text if the LLM
  misread. The mapping table lives in `EXTRACTION_SYSTEM_INSTRUCTION`.

**D7. Deadline missing or `/deadlines` shows nothing**
- *Where:* `pipeline.parse_date_safe` — **only accepts exact `YYYY-MM-DD`**; anything else
  ("Nov 30", "30/11/2026") silently becomes `NULL`.
- *Also:* jobs with status ARCHIVED/REJECTED are excluded from upcoming; expired jobs only
  appear via `get_expired_deadlines` (digest), not `/deadlines`.
- *Fix:* `update-profile`-style fix doesn't exist for jobs yet — update the DB directly or
  re-ingest; long-term improve date parsing (roadmap).

**D8. Match score lower than expected / ATS audit says a skill is "missing" that you clearly have**
- *Where:* `profile.evaluate_job_match` skill loop and `resume.audit_ats_keywords` — both now
  route through `skills.find_matching_skill` (alias-aware since v1.1).
- *Why it can still happen:* the alias pair for that skill isn't in `_SKILL_ALIASES`
  (e.g. a niche framework acronym), or the skill is genuinely absent from your profile.
- *Fix:* add the alias pair to `services/skills.py::_SKILL_ALIASES` (one line), or add the
  skill to `verified_skills` via `/addproject`/`update-profile`.

**D9. `/profile` shows wrong name/CGPA/default data**
- *Where:* `profile.get_candidate_profile` — **silently falls back to defaults** if
  `candidate_profile.json` is corrupt/unparseable.
- *Fix:* Validate the JSON: `python -c "import json;json.load(open('data/candidate_profile.json'))"`.
  Restore from git if broken.

**D10. Duplicate projects after `/addproject`**
- *Where:* `profile.add_project_to_profile` dedup — matches on identical github_url OR exact
  lowercase title. A renamed title or missing URL creates a second entry.
- *Fix:* Edit `candidate_profile.json` (carefully) and delete the stale folder in `data/projects/`.

**D11. PDF looks broken / text garbled / symbols replaced**
- *Where:* `pdf_generator.sanitize_text` — maps unicode (en/em-dash, smart quotes, bullets,
  arrows) to ASCII and XML-escapes `&<>`. Anything exotic (emoji in LLM bullets) may still
  render oddly in Helvetica.
- *Fix:* Check the `.md` companion (same content, no PDF layer) to isolate whether the issue
  is LLM content vs PDF rendering.

**D12. Telegram photo not processed**
- *Where:* `handle_photo_message` vs `handle_document_message`.
- *Why:* Photos sent as *files* (not compressed) arrive as documents; both handlers exist but
  MIME must be in `{png, jpeg, jpg, webp, pdf}` or `process_image_pipeline` raises ValueError.
- *Fix:* Send as photo, or check the extension.

**D13. `sqlite3` database is locked / weird concurrent behavior**
- *Why:* The bot (long-polling daemon) and a CLI command share `internships.db`. SQLite allows
  one writer; `check_same_thread=False` is set but write collisions can still occur.
- *Fix:* Don't run heavy CLI writes while the bot is mid-pipeline; retry. Long-term: WAL mode.

**D14. Digest is empty / says "no matching roles"**
- *Where:* `digest.build_daily_digest` — threshold **0.65**, only NEW/REVIEWED jobs, top 5.
- *Why:* All jobs already APPLIED+; or scores below 0.65 (see D8); or deadline window empty.
- *Fix:* Lower `match_threshold` param or ingest more jobs; check per-job scores in `notes`.

**D15. Search grounding returns nothing / wrong company**
- *Where:* `verifier.search_official_career_portal` — company-name-in-hostname check can
  false-positive (e.g. "meta" matching metacareers vs a random blog with "meta").
- *Why:* DDGS rate limits or generic company names ("Zoho", "Swiggy" fine; "Next" bad).
- *Fix:* Manually verify the URL; the `verification_source` note records what grounded it.

**D16. No digest at 8AM / reminders never arrive**
- *Where:* `bot.schedule_background_jobs` — job registration at startup.
- *Why:* (a) `python-telegram-bot[job-queue]` extra missing → JobQueue is None → warning in
  logs, `/digest` still works on-demand; (b) `SCHEDULER_ENABLED=false`; (c) bot process was
  restarted — pending jobs are in-memory only (no persistence); (d) `TZ_OFFSET_HOURS` wrong
  so "8AM" isn't your 8AM.
- *Diagnose:* `SELECT * FROM scheduler_log ORDER BY id DESC;` — if `daily_digest` runs fired,
  the send failed lower (see daemon logs, the jobs log failures via `logger.error`).
- *Rule:* resend-on-restart is intentional — no ledger entry ⇒ it re-sends. A digest already
  logged for today will NOT be re-sent after a restart.

**D17. Same reminder/digest sent twice**
- *Where:* two bot/daemon processes running simultaneously (two `run-bot` processes both
  poll Telegram AND both arm their own scheduler).
- *Why:* `has_scheduler_event` + insert is check-then-write with no DB unique constraint —
  two processes racing the same tick can both pass the check (window is small but real).
- *Fix:* kill the extra process (check Task Manager for two python processes). Long-term:
  unique constraint on `(event_type, event_key, sent_date)`.

**D18. `/watch add` fails / discovery sweep logs target errors**
- *Where:* discovery fetcher for that `target_type`.
- *Why:* (a) wrong board slug (must be the URL segment in `boards.greenhouse.io/<slug>` /
  `jobs.lever.co/<slug>` / `jobs.ashbyhq.com/<slug>`); (b) Greenhouse descriptions require a
  second GET per job (`_greenhouse_job_content`) — if that endpoint 429s, listing survives
  but description is empty; (c) custom-URL pages behind Cloudflare → scraper returns little;
  (d) company's board simply has no intern-flavored titles this week — `is_internship_title`
  filters strictly.
- *Diagnose:* `python -m src.interfaces.cli watch list` shows `last_error` per target.
- *Fix:* correct the slug (probe again with the same add command — dedup by (company, type,
  key) returns the existing target).

**D19. Discovery ingested a weird posting / missed an obvious one**
- *Where:* `discovery.py`: title filter → LLM parse → `_build_job_from_listing`.
- *Why:* Title without internship keywords is dropped intentionally; LLM slot failure
  (quota/429) → that posting skipped (None slot) while others ingest; SUSPICIOUS scam hits
  are ingested but never alerted; eligible-but-low-score postings are saved silently
  (below `DISCOVERY_ALERT_MIN_SCORE`).
- *Diagnose:* `/list` (or SQL) rows with `raw_ingestions.source_type = 'discovery'`; compare
  `notes` field match breakdown. Score stars are logged line-per-posting in bot pushes? No —
  non-alertable ones are silent by design. Lower `DISCOVERY_ALERT_MIN_SCORE` if too quiet.

### Logging
- The bot's `__main__` sets `logging.basicConfig(INFO)`. **The CLI does not configure logging** —
  pipeline warnings (fallbacks, grounding failures) are invisible unless you add:
  `python -m src.interfaces.cli extract-url <url>` after setting `logging` via env or running
  with `-O` removed… simplest: temporarily add `logging.basicConfig(level=logging.INFO)` in `cli.main()`.

---

## 8. Testing

- `pytest -v` → 111 tests, ~20s. No network, no API keys, no real DB.
- `ruff check src tests` → pinned critical-correctness rule set (`ruff.toml`: E4/E7/E9/F/I; the
  SQLAlchemy `!= None` IS-NOT-NULL idiom is whitelisted via E711/E712 ignores; `__init__.py`
  re-exports are exempt). CI runs it on every push/PR before pytest.
- `tests/conftest.py`: per-test in-memory SQLite engine (`StaticPool`), fresh schema, dropped after.
- Coverage by file: bot handlers (**incl. scheduler job registration** — build tests set
  `APP_ENV=testing` so no DB init happens), config, database (**dedup/merge/status/
  deadlines + scheduler ledger + watch targets**), digest (**incl. reminder formatter**),
  discovery (**fetchers, dedup, intake filter, alerting — `httpx` mocked**), extractor
  (mock client + **batch/list extraction**), github_service, models (**incl. system tables**),
  multimodal, pdf_generator, pipeline (mocked scrape/extract), profile (scoring/dedup),
  resume (alias-aware audit), scraper (mocked HTTP; **the 404/timeout tests also stub the
  DDGS fallback to stay offline**), skills (**alias matrix**), status tracking, verifier.
- **Convention when adding features:** every new service function gets a test file or joins an
  existing one; use the `session` fixture for DB work; mock Gemini via injected fake clients
  (see `test_extractor.py` / `test_multimodal.py` patterns); mock discovery HTTP by patching
  `src.services.discovery._http_get_json`.

---

## 9. Configuration & Secrets

`.env` (git-ignored) — loaded by `pydantic-settings` from `src/config.py`:

| Var | Used by | Behavior if missing |
|---|---|---|
| `GEMINI_API_KEY` | extractor, profile, github_service, resume, discovery | ValueError at first LLM call |
| `GEMINI_MODEL` | all LLM calls | Defaults `gemini-3.5-flash`; fallback chain still applies |
| `TELEGRAM_BOT_TOKEN` | bot | ValueError at startup |
| `TELEGRAM_ALLOWED_USER_ID` | bot auth | ⚠️ Bot becomes open to everyone |
| `DATABASE_URL` | database | Defaults `sqlite:///internships.db` |
| `APP_ENV` | config helpers (tests set `testing`) | Defaults `development` |
| `SCHEDULER_ENABLED` | bot `schedule_background_jobs` | Defaults `true`; `false` = no background jobs |
| `DIGEST_HOUR_LOCAL` | bot `run_daily` | Defaults `8` (local tz) |
| `TZ_OFFSET_HOURS` | bot scheduler tz | Defaults `5.5` (IST) |
| `DEADLINE_REMINDER_WINDOW_DAYS` | deadline sweep | Defaults `2` (≤2-day deadlines) |
| `DEADLINE_CHECK_INTERVAL_HOURS` | bot `run_repeating` | Defaults `4` |
| `DISCOVERY_ENABLED` | discovery sweep | Defaults `true` |
| `DISCOVERY_CHECK_INTERVAL_HOURS` | bot `run_repeating` | Defaults `2` |
| `DISCOVERY_ALERT_MIN_SCORE` | `is_alertable` gate | Defaults `0.65` |
| `DISCOVERY_MAX_PER_RUN` | per-target LLM budget | Defaults `8` |

⚠️ **Security incident log (2026-10-21):** `.env.example` was committed with real-looking
credentials. The file has been replaced with placeholders — **rotate the Gemini key and
Telegram bot token anyway**, since they remain in git history (commit `4ab5b9b`). If the repo
was ever pushed, treat both as compromised.

---

## 10. Known Gaps & Roadmap

Progress list — items move to Section 12 as they're built (baseline agreed 2026-10-21):

1. ~~**Scheduled 8AM digest does not exist**~~ — **DONE v1.1** (PTB JobQueue: 8AM digest,
   deadline reminders every 4h ≤2-day window, idempotent via `scheduler_log`).
2. ~~**Keyword matching is substring-only**~~ — **DONE v1.1** (`skills.py` alias engine in
   profile scoring + ATS audit).
3. ~~**Push-only ingestion**~~ — **LARGELY DONE v1.1** for ATS boards: `/watch` +
   `run_discovery` sweeps Ashby/Greenhouse/Lever/custom careers URLs proactively. Still open:
   Internshala/Unstop/Wellfound crawl ( scraping-heavy, needs per-site adapters).
4. **No Workday ATS resolver** despite `myworkdayjobs.com` being whitelisted as trusted.
5. **Blocking LLM/HTTP calls inside async bot handlers** — pipelines still run inline in
   handlers (scheduler callbacks already wrap work in `asyncio.to_thread`).
6. **~60 lines duplicated** across the three `process_*_pipeline` functions (verify/match/notes/save).
7. **Resumes not linked in DB** — generated files aren't recorded against job ids (no version history).
8. **No cover letter artifact**, no interview-prep generation, no referral tracker, no funnel analytics.
9. **`parse_date_safe` accepts only `YYYY-MM-DD`** (D7) — partially mitigated in discovery
   (`parse_deadline_any_format` handles month-names + DD/MM); legacy pipelines still strict.
10. Minor: dead code listed in Section 3; CLI lacks logging setup; SQLite not in WAL mode;
    `scheduler_log` has no DB unique constraint (two-process race, D17).

**Agreed priority order:** ~~Tier 1~~ (v1.1 complete) → **Tier 2** (semantic matching via
embeddings/ChromaDB, resume versioning, cover letters) → **Tier 3** (async pipeline, Workday
resolver, referral tracker, analytics, interview prep).

---

## 11. How To Explain This Project (Interview Cheat Sheet)

**One-liner:** "A local-first internship intelligence system: multimodal ingestion from
Telegram, LLM extraction with schema enforcement, authenticity verification against official
ATS portals, profile-based match scoring, and zero-fabrication ATS resume generation — all
tested, all free-tier."

**Architecture soundbite:** "Three ingestion pipelines converge on one orchestrator that
audit-logs raw input first, then extract → verify → score → dedup-merge into SQLite. The LLM
never returns free text — every call is Pydantic-schema-enforced, with a model fallback chain
for quota errors."

**Hardest problems solved (mention these):**
- Job portals behind Cloudflare → ATS public JSON APIs + search-grounding fallback
- LLM hallucination → zero-fabrication constraint enforced at prompt, schema, and audit layers
- Duplicate postings from forwarded messages → normalized dedup with field-level merge
- Single-page PDF fit → dynamic bullet-depth calibration in ReportLab
- Digests/reminders firing twice → idempotency ledger (`scheduler_log`) keyed on (event, day)
- Stale LLM keyword matching ("DRF" vs "Django") → alias canonicalization with anti-containment guards
- Finding internships nobody forwarded → watchlist of ATS public JSON endpoints swept hourly,
  one batched LLM call per board instead of per posting

---

## 12. Change Log

| Date | Version | Changes |
|---|---|---|
| 2026-10-24 | v1.2 | **Hardening + headless-Chromium backend + CI (111 tests):** ① Optional Playwright renderer in `scraper.py` — Workday (`myworkdayjobs.com`) and any thin-SPA/403/429 URL render in local headless Chromium (images/media/fonts blocked, 50-char quality gate) before DuckDuckGo search-grounding; `PLAYWRIGHT_ENABLED` toggle, dependency stays optional. ② Security — PII profile/resumes/PDFs are git-ignored and untracked; sanitized `candidate_profile.example.json` shipped; bot is deny-by-default when `TELEGRAM_ALLOWED_USER_ID` is unset (LOCKED banner, warning on every rejected message); upload filenames sanitized; HTTPS-only fetch contract for the scraper. ③ Correctness — digest urgent-actions exclude applied/interviewing/offered jobs; digest + reminder idempotency is check-before-send/log-after-success; all deadline logic uses a new `today_local()` helper (`TZ_OFFSET_HOURS`) instead of host-local `date.today()`; corrupt profile JSON raises loudly; ATS audit reports 0% (not 100%) when no keywords extract; scam verdicts no longer propagate scam URLs as `official_portal_url`. ④ Verifier rewrite — hostname/subdomain-exact trusted-ATS matching and token-boundary company↔domain matching (blocked `evilgreenhouse.io`/`noamazon.com` spoofing), search-grounding promotions validated the same way. ⑤ Performance/robustness — all blocking Gemini/HTTP/PDF handler calls wrapped in `asyncio.to_thread`; `find_existing_job` projects only `(id, company_name, role_title)`; ATS resolver IndexError guards; Gemini transient-error token classifier. ⑥ CI — GitHub Actions workflow (`.github/workflows/ci.yml`) runs `ruff check src tests` (pinned critical ruleset in new `ruff.toml`, ~65 pre-existing lint debts fixed) + the full pytest suite on every push/PR; Python 3.11 on ubuntu-latest, no secrets required. |
| 2026-10-21 | v1.1 | **Tier 1 shipped (99 tests):** ① Real background scheduler — PTB JobQueue arms 8AM digest (`DIGEST_HOUR_LOCAL`), deadline reminders every 4h (≤2-day window) and discovery sweeps every 2h; idempotent via new `scheduler_log` table; reminders get urgency-grouped HTML. ② Skill alias engine `services/skills.py` (~40 alias groups + anti-containment pairs) wired into match scoring and ATS audit — DRF↔Django REST, K8s↔Kubernetes, Go↔Golang now match. ③ Proactive discovery `services/discovery.py` — `/watch` (CLI + Telegram) manages a `watch_targets` watchlist of Ashby/Greenhouse/Lever boards or any careers URL; `run_discovery` sweeps boards, filters intern/trainee titles, batch-extracts details in ONE LLM call per target, verifies, scores, ingests with `source_type=discovery`, and pushes only alert-worthy postings (≥0.65, not scam, not ineligible) — everything else saved silently. Manual `/discover` command too. Batch extractors added (`extract_multiple_jobs_from_text`, `extract_jobs_list_from_text`). Flaky scraper tests stubbed off the live DDGS fallback. |
| 2026-10-21 | v1.0 | Baseline documented ("Working MVP", commit `4ab5b9b`). Sanitized `.env.example` (real credentials removed — **rotate keys**). |
