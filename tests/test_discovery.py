"""Tests for Proactive Discovery Engine (board fetchers + run orchestration)."""

from typing import Dict

from sqlmodel import Session

from src.database import add_watch_target, list_watch_targets
from src.models.job import (
    ApplicationStatus,
    EligibilityStatus,
    JobPosting,
    VerificationStatus,
)
from src.services import discovery
from src.services.discovery import (
    is_alertable,
    is_internship_title,
    parse_deadline_any_format,
    run_discovery,
)
from src.services.extractor import ExtractedJobData


def make_extracted(role: str = "Machine Learning Intern", skills=None) -> ExtractedJobData:
    return ExtractedJobData(
        company_name="Any",
        role_title=role,
        locations=["Remote"],
        required_skills=skills if skills is not None else ["Python", "SQL"],
        preferred_skills=["Docker"],
        allowed_grad_years=[2028],
        eligibility_notes="",
        confidence_score=0.9,
    )


# ==============================================================================
# Title filtering & deadline parsing
# ==============================================================================

def test_is_internship_title_decisions():
    assert is_internship_title("Software Engineering Intern") is True
    assert is_internship_title("Graduate Software Engineer") is True
    assert is_internship_title("2027 Summer Trainee Program") is True
    assert is_internship_title("Backend Co-op Student") is True
    assert is_internship_title("Senior Staff Engineer") is False
    assert is_internship_title("Engineering Manager, AI Platform") is False
    assert is_internship_title("PhD Research Intern") is False
    assert is_internship_title("Director of Engineering") is False
    assert is_internship_title("Data Analyst II") is False
    assert is_internship_title("") is False
    assert is_internship_title(None) is False


def test_parse_deadline_any_format():
    assert parse_deadline_any_format("2026-11-02T05:00:00+05:30") == "2026-11-02"
    assert parse_deadline_any_format("2026-11-02") == "2026-11-02"
    assert parse_deadline_any_format("closes on 2026-11-02 !!") == "2026-11-02"
    assert parse_deadline_any_format("November 2nd") is None
    assert parse_deadline_any_format(None) is None
    assert parse_deadline_any_format(12345) is None


# ==============================================================================
# Board fetchers (mocked HTTP)
# ==============================================================================

def test_fetch_lever_board_parsing(monkeypatch):
    fake = [
        {
            "id": "a1", "text": "Backend Engineering Intern",
            "applyUrl": "https://jobs.lever.co/x/a1/apply", "hostedUrl": "https://jobs.lever.co/x/a1",
            "descriptionPlain": "Build Python REST services.",
            "categories": {"location": "Remote", "workplaceType": "Remote"},
        },
        {
            "id": "a2", "text": "Senior Platform Engineer",
            "applyUrl": "https://jobs.lever.co/x/a2/apply", "hostedUrl": "https://jobs.lever.co/x/a2",
            "descriptionPlain": "",
            "categories": {"location": "SF", "workplaceType": "Hybrid"},
        },
    ]
    monkeypatch.setattr(discovery, "_http_get_json", lambda url, timeout=discovery.DEFAULT_TIMEOUT: fake)

    listings = discovery.fetch_lever_board("x")
    assert len(listings) == 2
    assert listings[0].title == "Backend Engineering Intern"
    assert listings[0].locations == ["Remote"]
    assert listings[0].application_url == "https://jobs.lever.co/x/a1/apply"
    assert "Python REST" in listings[0].description_text
    assert listings[1].board_ref == "a2"


def test_fetch_greenhouse_board_parsing(monkeypatch):
    fake = {
        "jobs": [
            {
                "id": 111,
                "title": "Data Engineering Intern",
                "absolute_url": "https://stripe.com/jobs/search?gh_jid=111",
                "location": {"name": "Remote Asia"},
                "company_name": "Stripe",
            },
        ]
    }
    monkeypatch.setattr(discovery, "_http_get_json", lambda url, timeout=discovery.DEFAULT_TIMEOUT: fake)

    listings = discovery.fetch_greenhouse_board("stripe")
    assert listings[0].title == "Data Engineering Intern"
    assert listings[0].locations == ["Remote Asia"]
    assert listings[0].board_ref == "111"
    assert listings[0].application_url.endswith("gh_jid=111")


def test_fetch_ashby_board_parsing(monkeypatch):
    fake = {
        "jobs": [
            {
                "id": "uuid-1",
                "title": "Frontend Engineering Intern",
                "isListed": True,
                "location": "Europe",
                "secondaryLocations": ["London", {"location": "Remote"}],
                "applyUrl": "https://jobs.ashbyhq.com/l/uuid-1",
                "jobUrl": "https://jobs.ashbyhq.com/l/uuid-1",
                "descriptionPlain": "Ship React features.",
            },
            {
                "id": "uuid-2",
                "title": "Unlisted Draft Role",
                "isListed": False,
                "location": "X",
                "secondaryLocations": [],
                "applyUrl": "", "jobUrl": "", "descriptionPlain": "",
            },
        ]
    }
    monkeypatch.setattr(discovery, "_http_get_json", lambda url, timeout=discovery.DEFAULT_TIMEOUT: fake)

    listings = discovery.fetch_ashby_board("l")
    assert len(listings) == 1
    assert listings[0].title == "Frontend Engineering Intern"
    assert "London" in listings[0].locations and "Remote" in listings[0].locations


# ==============================================================================
# End-to-end discovery run (mocked HTTP + mocked LLM enrichment)
# ==============================================================================

def test_run_discovery_ingests_new_and_dedupes(session: Session, monkeypatch):
    add_watch_target(session, "Spotify", "lever", "spotify")

    fake_board = [
        {
            "id": "1", "text": "Machine Learning Intern",
            "applyUrl": "https://jobs.lever.co/spotify/1/apply", "hostedUrl": "https://jobs.lever.co/spotify/1",
            "descriptionPlain": "Train ML models using Python and SQL pipelines.",
            "categories": {"location": "Remote", "workplaceType": "Remote"},
        },
        {
            "id": "2", "text": "Senior Staff Backend Engineer",
            "applyUrl": "https://jobs.lever.co/spotify/2/apply", "hostedUrl": "https://jobs.lever.co/spotify/2",
            "descriptionPlain": "Experienced only.",
            "categories": {"location": "NYC", "workplaceType": "Hybrid"},
        },
    ]
    monkeypatch.setattr(discovery, "_http_get_json", lambda url, timeout=discovery.DEFAULT_TIMEOUT: fake_board)
    monkeypatch.setattr(discovery, "extract_multiple_jobs_from_text", lambda blocks, client=None: [make_extracted() for _ in blocks])

    outcomes_first = run_discovery(session)
    assert len(outcomes_first) == 1
    job = outcomes_first[0].job
    assert job.company_name == "Spotify"
    assert job.role_title == "Machine Learning Intern"
    assert job.is_official_portal is True
    assert job.verification_status == VerificationStatus.VERIFIED_OFFICIAL
    assert "Python" in job.required_skills
    assert job.eligibility_status == EligibilityStatus.ELIGIBLE
    assert "Discovered via watch" in (job.notes or "")

    # Second identical run: the same posting must NOT duplicate
    outcomes_second = run_discovery(session)
    assert outcomes_second == []


def test_run_discovery_greenhouse_content_enrichment(session: Session, monkeypatch):
    add_watch_target(session, "Stripe", "greenhouse", "stripe")

    def fake_http(url: str, timeout=discovery.DEFAULT_TIMEOUT) -> Dict:
        if "jobs/stripe/" in url or "/jobs/111" in url:
            return {"content": "<p><h1>Machine Learning Intern</h1>Use <b>Python</b> and SQL.</p>"}
        return {
            "jobs": [
                {"id": 111, "title": "Machine Learning Intern",
                 "absolute_url": "https://stripe.com/jobs?gh_jid=111",
                 "location": {"name": "Remote"}, "company_name": "Stripe"},
            ]
        }
    monkeypatch.setattr(discovery, "_http_get_json", fake_http)
    monkeypatch.setattr(discovery, "fetch_greenhouse_job_content", lambda token, ref, timeout=discovery.DEFAULT_TIMEOUT: "Machine Learning Intern: use Python, SQL, and Docker.")

    monkeypatch.setattr(discovery, "extract_multiple_jobs_from_text", lambda blocks, client=None: [make_extracted("Machine Learning Intern")])

    outcomes = run_discovery(session)
    assert len(outcomes) == 1
    job = outcomes[0].job
    assert job.company_name == "Stripe"
    assert "Python" in job.required_skills
    assert outcomes[0].description_text and "Python" in outcomes[0].description_text


def test_run_discovery_skips_scam_and_stores_suspicious(session: Session, monkeypatch):
    add_watch_target(session, "Sketchy", "lever", "sketchy")
    fake_board = [
        {
            "id": "9", "text": "Work From Home Intern",
            "applyUrl": "https://t.me/scam/9", "hostedUrl": "https://t.me/scam/9",
            "descriptionPlain": "Pay registration fee 500 rupees and deposit money to start. Contact us on whatsapp only!",
            "categories": {"location": "Remote", "workplaceType": "Remote"},
        },
    ]
    monkeypatch.setattr(discovery, "_http_get_json", lambda url, timeout=discovery.DEFAULT_TIMEOUT: fake_board)
    monkeypatch.setattr(discovery, "extract_multiple_jobs_from_text", lambda blocks, client=None: [make_extracted("Work From Home Intern") for _ in blocks])

    outcomes = run_discovery(session)
    assert len(outcomes) == 1
    assert outcomes[0].job.verification_status == VerificationStatus.SUSPICIOUS
    assert is_alertable(outcomes[0]) is False


def test_is_alertable_rules():
    from src.services.discovery import DiscoveredJobOutcome
    job_ok = JobPosting(
        company_name="A", role_title="X Intern",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        verification_status=VerificationStatus.VERIFIED_OFFICIAL,
        application_status=ApplicationStatus.NEW,
    )
    assert is_alertable(DiscoveredJobOutcome(job=job_ok, is_new=True, match_score=0.85)) is True

    low_score_job = JobPosting(
        company_name="B", role_title="Y Intern",
        eligibility_status=EligibilityStatus.UNCERTAIN,
        verification_status=VerificationStatus.UNVERIFIED_THIRD_PARTY,
        application_status=ApplicationStatus.NEW,
    )
    assert is_alertable(DiscoveredJobOutcome(job=low_score_job, is_new=True, match_score=0.40)) is False

    scam = JobPosting(
        company_name="C", role_title="Z Intern",
        eligibility_status=EligibilityStatus.ELIGIBLE,
        verification_status=VerificationStatus.SUSPICIOUS,
        application_status=ApplicationStatus.NEW,
    )
    assert is_alertable(DiscoveredJobOutcome(job=scam, is_new=True, match_score=0.9)) is False


def test_run_discovery_no_targets_is_noop(session: Session):
    assert run_discovery(session) == []


def test_run_discovery_failure_isolation(session: Session, monkeypatch):
    add_watch_target(session, "Broken", "lever", "brokenslug")
    add_watch_target(session, "Fine", "lever", "fineslug")

    def fake_http(url: str, timeout=discovery.DEFAULT_TIMEOUT):
        if "brokenslug" in url:
            raise RuntimeError("api down")
        return [
            {"id": "5", "text": "QA Intern", "descriptionPlain": None,
             "categories": {"location": "Remote"}, "applyUrl": "u", "hostedUrl": "u"},
        ]

    monkeypatch.setattr(discovery, "_http_get_json", fake_http)
    monkeypatch.setattr(discovery, "extract_multiple_jobs_from_text", lambda blocks, client=None: [None])

    outcomes = run_discovery(session)
    targets = {t.company_name: t for t in list_watch_targets(session, active_only=False)}
    assert targets["Broken"].last_error and "api down" in targets["Broken"].last_error
    assert targets["Fine"].last_error is None
    assert len(outcomes) == 1 and outcomes[0].job.company_name == "Fine"
