"""Tests for P1 Networking: Contact book, enrichment, alumni finder, approved outreach."""

from datetime import date, timedelta
from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import Session, select

from src.config import settings
from src.database import count_scheduler_events
from src.models.contact import Contact, ContactSource, EmailStatus, OutreachStatus
from src.models.job import JobPosting
from src.models.system import SchedulerEventType
from src.services.alumni_finder import (
    AlumniFinderError,
    build_linkedin_search_urls,
    find_engineers_on_github,
)
from src.services.contact_enrichment import (
    ProspeoError,
    enrich_linkedin_profile,
    enrichment_available,
)
from src.services.outreach import (
    CAP_LEDGER_KEY,
    OutreachError,
    approve_and_send,
    craft_outreach_draft,
    get_contacts_needing_followup,
    mark_contact_replied,
    upsert_contact,
)


class FakeSMTP:
    """Context-manager SMTP recording sendmail calls (factory())."""

    instances = []

    def __init__(self):
        self.login_calls = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def login(self, user, password):
        self.login_calls.append(user)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append({"from": from_addr, "to": to_addrs, "msg": msg})


def make_contact(session: Session, email="sarah@google.com", name="Sarah Recruiter", **kw) -> Contact:
    contact = Contact(
        full_name=name,
        company_name=kw.pop("company_name", "Google"),
        role_title=kw.pop("role_title", "Recruiter"),
        relationship=kw.pop("relationship", "recruiter"),
        email=email,
        **kw,
    )
    session.add(contact)
    session.commit()
    session.refresh(contact)
    return contact


def make_job(session: Session, company="Google", role="SWE Intern") -> JobPosting:
    job = JobPosting(company_name=company, role_title=role, required_skills=["Python"])
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


# ==============================================================================
# Contact book (upsert / dedupe)
# ==============================================================================

def test_upsert_contact_creates_new(session: Session):
    contact, created = upsert_contact(
        session, full_name="Jane Doe", company_name="Stripe", email="jane@stripe.com", relationship="alumni"
    )
    assert created is True
    assert contact.id is not None
    assert contact.source == ContactSource.MANUAL
    assert contact.outreach_status == OutreachStatus.NONE


def test_upsert_contact_merges_by_email(session: Session):
    contact, created = upsert_contact(session, full_name="Jane Doe", email="jane@stripe.com")
    contact2, created2 = upsert_contact(
        session, full_name="Different Name", email="jane@stripe.com", company_name="Stripe", role_title="Recruiter"
    )
    assert created2 is False
    assert contact2.id == contact.id
    # blank-worthy merge: only fills blanks, never overwrites
    assert contact2.full_name == "Jane Doe"
    assert contact2.company_name == "Stripe"
    assert contact2.role_title == "Recruiter"


def test_upsert_contact_merges_by_linkedin(session: Session):
    first, created = upsert_contact(
        session, full_name="LinkedIn Person", linkedin_url="https://linkedin.com/in/lp"
    )
    second, created2 = upsert_contact(
        session, full_name="LinkedIn Person", email="lp@corp.com",
        linkedin_url="https://linkedin.com/in/lp", email_status=EmailStatus.PROSPEO_VERIFIED,
    )
    assert created2 is False
    assert second.id == first.id
    assert second.email == "lp@corp.com"
    assert second.email_status == EmailStatus.PROSPEO_VERIFIED


def test_upsert_merge_does_not_downgrade_status(session: Session):
    contact, _ = upsert_contact(session, full_name="Jane", email="jane@x.com", email_status=EmailStatus.REPLIED)
    plain, _ = upsert_contact(session, full_name="Jane", email="jane@x.com")
    assert plain.email_status == EmailStatus.REPLIED  # unverified never downgrades a reply
    prospeo, _ = upsert_contact(
        session, full_name="Jane", email="jane@x.com", email_status=EmailStatus.PROSPEO_VERIFIED
    )
    assert prospeo.email_status == EmailStatus.PROSPEO_VERIFIED  # verification upgrade wins


# ==============================================================================
# Approved sending pipeline
# ==============================================================================

def _gmail_env(monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_ADDRESS", "candidate.test@gmail.com")
    monkeypatch.setattr(settings, "GMAIL_APP_PASSWORD", "app-password-1234")


def test_approve_and_send_happy_path(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    contact = make_contact(session)

    updated = approve_and_send(
        session, contact.id, "Hello for internship", "Hi Sarah,\nI would love to apply.",
        smtp_factory=FakeSMTP,
    )

    assert updated.outreach_status == OutreachStatus.SENT
    assert updated.sent_at is not None
    today = date.today()
    assert updated.followup_at == (today + timedelta(days=settings.OUTREACH_FOLLOWUP_DAYS)).isoformat()
    assert len(FakeSMTP.instances[-1].sent) == 1
    assert FakeSMTP.instances[-1].sent[0]["to"] == ["sarah@google.com"]


def test_approve_and_send_requires_email(session: Session):
    contact = make_contact(session, email=None)
    with pytest.raises(OutreachError, match="no email"):
        approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)


def test_approve_and_send_missing_credentials(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(settings, "GMAIL_APP_PASSWORD", "")
    contact = make_contact(session)
    with pytest.raises(OutreachError, match="Gmail delivery is not configured"):
        approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)


def test_approve_and_send_daily_cap(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    monkeypatch.setattr(settings, "OUTREACH_MAX_FIRST_SENDS_PER_DAY", 1)
    for _ in range(2):
        make_contact(session)

    first_contact = session.exec(select(Contact)).first()
    approve_and_send(session, first_contact.id, "s", "b", smtp_factory=FakeSMTP)

    second_contact = session.exec(select(Contact).order_by(Contact.id.desc())).first()
    with pytest.raises(OutreachError, match="Daily cap reached"):
        approve_and_send(session, second_contact.id, "s", "b", smtp_factory=FakeSMTP)


def test_approve_and_send_duplicate_same_day_blocked(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    contact = make_contact(session)
    approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)
    with pytest.raises(OutreachError, match="already emailed today"):
        approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)


def test_daily_cap_ledger_logged(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    contact = make_contact(session)
    approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)
    assert count_scheduler_events(session, SchedulerEventType.OUTREACH_FIRST_SEND, CAP_LEDGER_KEY, date.today()) == 1


def test_followup_flow_marks_and_frees_followup_date(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    contact = make_contact(session)
    approve_and_send(session, contact.id, "first", "b", smtp_factory=FakeSMTP)  # first send (status SENT, followup_at +7d)
    contact.followup_at = date.today().isoformat()
    session.add(contact)
    session.commit()

    due = get_contacts_needing_followup(session)
    assert due and due[0][0].id == contact.id

    approve_and_send(session, contact.id, "checking in", "body", followup=True, smtp_factory=FakeSMTP)
    session.refresh(contact)
    assert contact.outreach_status == OutreachStatus.FOLLOWED_UP
    assert count_scheduler_events(session, SchedulerEventType.OUTREACH_FOLLOWUP_SEND, CAP_LEDGER_KEY, date.today()) == 1


def test_get_contacts_needing_followup_filters_and_sorts(session: Session):
    # SENT + overdue by 3
    c1 = make_contact(session, email="a@x.com")
    c1.outreach_status, c1.followup_at = OutreachStatus.SENT, (date.today() - timedelta(days=3)).isoformat()
    # SENT + due later
    c2 = make_contact(session, email="b@x.com")
    c2.outreach_status, c2.followup_at = OutreachStatus.SENT, (date.today() + timedelta(days=2)).isoformat()
    # SENT but no followup_at
    c3 = make_contact(session, email="c@x.com")
    c3.outreach_status = OutreachStatus.SENT
    # REPLIED -> never re-surfaced
    c4 = make_contact(session, email="d@x.com")
    c4.outreach_status = OutreachStatus.REPLIED
    # followup already done
    c5 = make_contact(session, email="e@x.com")
    c5.outreach_status = OutreachStatus.FOLLOWED_UP
    session.add_all([c1, c2, c3, c4, c5])
    session.commit()

    due = get_contacts_needing_followup(session)
    assert [c.id for c, _o in due] == [c1.id]
    assert due[0][1] == 3


def test_mark_contact_replied(session: Session, monkeypatch):
    _gmail_env(monkeypatch)
    contact = make_contact(session)
    approve_and_send(session, contact.id, "s", "b", smtp_factory=FakeSMTP)
    replied = mark_contact_replied(session, contact.id)
    assert replied.outreach_status == OutreachStatus.REPLIED
    assert replied.email_status == EmailStatus.REPLIED
    assert replied.replied_at is not None


# ==============================================================================
# LLM draft with truthful-template fallback
# ==============================================================================

def test_craft_outreach_draft_template_fallback(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    contact = make_contact(session, name="Sarah Recruiter", email="sarah@g.com")
    job = make_job(session, company="Google", role="Software Engineering Intern")

    draft = craft_outreach_draft(contact, job, client=None)
    assert draft.is_followup is False
    assert draft.contact_id == contact.id
    assert "Sarah" in draft.body
    assert "Google" in draft.body
    assert draft.subject


def test_craft_outreach_draft_followup_template(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    contact = make_contact(session, name="Aisha Nair", email="aisha@stripe.com")
    job = make_job(session, company="Stripe", role="Backend Intern")

    draft = craft_outreach_draft(contact, job, followup=True, client=None)
    assert draft.is_followup is True
    assert "Aisha" in draft.body
    assert "Stripe" in draft.body


def test_craft_outreach_draft_uses_llm_response(session: Session, monkeypatch):
    contact = make_contact(session)
    job = make_job(session)

    fake_client = MagicMock()
    resp = MagicMock()
    resp.text = '{"subject": "Hello from Gemini", "body": "Hi Sarah, real draft."}'
    fake_client.models.generate_content.return_value = resp

    draft = craft_outreach_draft(contact, job, client=fake_client)
    assert draft.subject == "Hello from Gemini"
    assert "real draft" in draft.body
    assert fake_client.models.generate_content.called


def test_craft_outreach_draft_llm_failure_falls_back(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    contact = make_contact(session, name="Priya Shah")
    job = make_job(session)

    bad_client = MagicMock()
    bad_client.models.generate_content.side_effect = RuntimeError("quota exhausted")

    draft = craft_outreach_draft(contact, job, client=bad_client)
    assert "Priya" in draft.body  # fell back to template
    assert bad_client.models.generate_content.called


# ==============================================================================
# Prospeo enrichment adapter
# ==============================================================================

def test_validate_prospero_api_key_rejects_empty(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "")
    assert enrichment_available() is False
    with pytest.raises(ValueError, match="missing"):
        enrich_linkedin_profile("https://linkedin.com/in/test")


def test_validate_prospero_api_key_rejects_placeholder_and_short(monkeypatch):
    with pytest.raises(ValueError):
        enrich_linkedin_profile("https://linkedin.com/in/test", api_key="your-prospeo-key-here")
    with pytest.raises(ValueError, match="truncated"):
        enrich_linkedin_profile("https://linkedin.com/in/test", api_key="shortkey123")


def test_enrichment_available_true_with_key(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    assert enrichment_available() is True


def _prospeo_response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    return response


def test_enrich_linkedin_profile_success(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.return_value = _prospeo_response(200, {"data": {"full_name": "Sarah K", "email": "sarah.k@google.com", "job_title": "Tech Recruiter", "company": "Google"}})

    enriched = enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)
    assert enriched.email == "sarah.k@google.com"
    assert enriched.full_name == "Sarah K"
    assert enriched.company_name == "Google"
    assert enriched.email_status == EmailStatus.PROSPEO_VERIFIED
    args, kwargs = fake.post.call_args
    assert "api.prospeo.io" in args[0]
    assert kwargs["headers"]["X-KEY"] == "pk_live_9f8e7d6c5b4a3210"


def test_enrich_linkedin_profile_error_code(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.return_value = _prospeo_response(400, {"error_code": "API_KEY_INVALID"})
    with pytest.raises(ProspeoError, match="rejected the API key"):
        enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)


def test_enrich_linkedin_profile_generic_error(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.return_value = _prospeo_response(400, {"error_code": "NO_RESULT"})
    with pytest.raises(ProspeoError, match="NO_RESULT"):
        enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)


def test_enrich_linkedin_profile_no_email(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.return_value = _prospeo_response(200, {"data": {"full_name": "Sarah K"}})
    with pytest.raises(ProspeoError, match="no verified email"):
        enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)


def test_enrich_linkedin_profile_non_json(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.return_value = _prospeo_response(502, {})
    fake.post.return_value.json.side_effect = ValueError("bad json")
    with pytest.raises(ProspeoError, match="non-JSON"):
        enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)


def test_enrich_linkedin_profile_bad_url():
    with pytest.raises(ValueError, match="LinkedIn"):
        enrich_linkedin_profile("https://twitter.com/sarah", api_key="pk_live_9f8e7d6c5b4a3210")


def test_enrich_linkedin_profile_http_error(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "pk_live_9f8e7d6c5b4a3210")
    fake = MagicMock()
    fake.post.side_effect = httpx.ConnectError("no network")
    with pytest.raises(ProspeoError, match="request failed"):
        enrich_linkedin_profile("https://linkedin.com/in/sarahk", http_client=fake)


# ==============================================================================
# Alumni/GitHub discovery sweep
# ==============================================================================

def test_find_engineers_on_github_success():
    search_resp = MagicMock()
    search_resp.status_code = 200
    search_resp.json.return_value = {"total_count": 1, "items": [{"login": "jsmith"}]}

    detail_resp = MagicMock()
    detail_resp.status_code = 200
    detail_resp.json.return_value = {
        "login": "jsmith", "name": "John Smith", "company": "@Stripe",
        "email": "js@stripe.com", "html_url": "https://github.com/jsmith", "bio": "Intern host",
    }

    def fake_get(url, **kwargs):
        return detail_resp if "/users/" in url and "/search/" not in url else search_resp

    http_client = MagicMock()
    http_client.get.side_effect = fake_get

    contacts = find_engineers_on_github("stripe", http_client=http_client)
    assert len(contacts) == 1
    assert contacts[0].full_name == "John Smith"
    assert contacts[0].email == "js@stripe.com"
    assert "intern" in contacts[0].bio_matches
    search_call = http_client.get.call_args_list[0]
    assert "stripe in:company type:users" in search_call.kwargs["params"]["q"]


def test_find_engineers_drops_companyless_profiles():
    search_resp = MagicMock()
    search_resp.status_code = 200
    search_resp.json.return_value = {"items": [{"login": "a"}, {"login": "b"}]}

    def fake_get(url, **kwargs):
        detail = MagicMock()
        detail.status_code = 200
        login = url.rsplit("/", 1)[-1]
        detail.json.return_value = {"login": login, "company": "" if login == "b" else "Stripe"}
        return detail

    search_resp = MagicMock()
    search_resp.status_code = 200
    search_resp.json.return_value = {"items": [{"login": "a"}, {"login": "b"}]}
    http_client = MagicMock()

    def side_effect(url, **kwargs):
        if "/search/" in url:
            return search_resp
        return fake_get(url, **kwargs)

    http_client.get.side_effect = side_effect
    contacts = find_engineers_on_github("stripe", http_client=http_client)
    assert [c.login for c in contacts] == ["a"]


def test_find_engineers_rate_limit_message():
    resp = MagicMock()
    resp.status_code = 403
    http_client = MagicMock()
    http_client.get.return_value = resp

    with pytest.raises(AlumniFinderError, match="rate limit"):
        find_engineers_on_github("stripe", http_client=http_client)


def test_find_engineers_http_error_wrapped():
    http_client = MagicMock()
    http_client.get.side_effect = httpx.ConnectError("down")
    with pytest.raises(AlumniFinderError, match="search failed"):
        find_engineers_on_github("stripe", http_client=http_client)


def test_build_linkedin_search_urls_contains_company_and_school():
    urls = build_linkedin_search_urls("Stripe")
    assert "Stripe" in urls["recruiters"] and "recruiter" in urls["recruiters"]
    assert "Vellore%20Institute%20of%20Technology" in urls["alumni"].replace("+", "%20")
    assert "engineer" in urls["engineers"]
