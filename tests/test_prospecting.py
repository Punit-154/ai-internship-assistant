"""Tests for P1.2 Prospeo Search-Person: recruiter discovery via /prospect."""

from unittest.mock import MagicMock

import httpx
import pytest
from sqlmodel import Session

from src.config import settings
from src.services.contact_enrichment import (
    CODE_BAD_RESPONSE,
    CODE_INSUFFICIENT_CREDITS,
    CODE_INVALID_API_KEY,
    CODE_NETWORK,
    CODE_RATE_LIMITED,
    ProspeoError,
    _reset_throttle_state,
    enrich_person,
)
from src.services.prospecting import (
    CODE_INVALID_FILTERS,
    CODE_NO_RESULTS,
    CODE_PLAN_REQUIRED,
    DEFAULT_RECRUITER_TITLES,
    ProspectCandidate,
    _build_search_filters,
    _company_filter,
    enrich_prospect,
    search_recruiters,
)

KEY = "pk_live_9f8e7d6c5b4a3210"

VERIFY_ONLY_FILTER = {"person_contact_details": {"email": ["VERIFIED"]}}

SEARCH_BODY = {
    "error": False,
    "free": False,
    "results": [
        {
            "person": {
                "person_id": "p1",
                "full_name": "Jane Doe",
                "current_job_title": "Technical Recruiter",
                "linkedin_url": "https://www.linkedin.com/in/janedoe",
                "email": {"revealed": False, "email": "jane.doe@stripe.com", "status": "VERIFIED"},
            },
            "company": {"name": "Stripe", "domain": "stripe.com"},
        },
        {
            "person": {
                "person_id": "p2",
                "full_name": "John TA",
                "current_job_title": "Talent Acquisition Lead",
                "linkedin_url": "https://www.linkedin.com/in/johnta",
            },
            "company": {"name": "Stripe"},
        },
    ],
    "pagination": {"current_page": 1, "per_page": 25, "total_page": 1, "total_count": 12},
}

ENRICH_BODY = {
    "error": False,
    "free_enrichment": False,
    "person": {
        "person_id": "p1",
        "full_name": "Jane Doe",
        "current_job_title": "Technical Recruiter",
        "linkedin_url": "https://www.linkedin.com/in/janedoe",
        "email": {"status": "VERIFIED", "revealed": True, "email": "jane.doe@stripe.com"},
    },
    "company": {"name": "Stripe", "domain": "stripe.com"},
}


@pytest.fixture(autouse=True)
def _no_prospeo_throttle(monkeypatch):
    """Keep adapter tests fast: no real sleeping + a clean throttle clock per test."""
    monkeypatch.setattr(settings, "PROSPEO_SEARCH_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(settings, "PROSPEO_MIN_INTERVAL_SECONDS", 0.0)
    _reset_throttle_state()
    yield
    _reset_throttle_state()


def _response(status, payload):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    return response


# ==============================================================================
# Company/targeting filter builders
# ==============================================================================

def test_company_filter_domains_vs_names():
    assert _company_filter("https://www.stripe.com/careers") == {"websites": {"include": ["stripe.com"]}}
    assert _company_filter("http://stripe.com?utm=1") == {"websites": {"include": ["stripe.com"]}}
    assert _company_filter("stripe.com") == {"websites": {"include": ["stripe.com"]}}
    assert _company_filter("Stripe") == {"names": {"include": ["Stripe"]}}
    assert _company_filter("Razorpay India") == {"names": {"include": ["Razorpay India"]}}


def test_company_filter_requires_input():
    with pytest.raises(ValueError, match="company name or careers domain"):
        _company_filter("")


def test_build_search_filters_default_recruiter_titles():
    filters = _build_search_filters("Stripe")
    assert filters["company"] == {"names": {"include": ["Stripe"]}}
    assert filters["person_job_title"] == {
        "include": list(DEFAULT_RECRUITER_TITLES),
        "match_mode": "CONTAINS",
    }
    assert filters["person_contact_details"] == VERIFY_ONLY_FILTER["person_contact_details"]


def test_build_search_filters_keyword_replaces_titles():
    filters = _build_search_filters("Stripe", keyword="engineer")
    assert filters["person_job_title"] == {"include": ["engineer"], "match_mode": "CONTAINS"}


def test_build_search_filters_verified_only_can_be_dropped():
    filters = _build_search_filters("Stripe", verified_only=False)
    assert "person_contact_details" not in filters


# ==============================================================================
# search_recruiters: request shape, parsing, credits
# ==============================================================================

def test_search_recruiters_request_shape_and_parse(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, SEARCH_BODY)

    page = search_recruiters("Stripe", http_client=fake)

    assert page.candidates[0].person_id == "p1"
    assert page.candidates[0].person_id == "p1"
    candidates = page.candidates
    assert [c.full_name for c in candidates] == ["Jane Doe", "John TA"]
    assert candidates[0].role_title == "Technical Recruiter"
    assert candidates[0].linkedin_url == "https://www.linkedin.com/in/janedoe"
    assert candidates[1].company_name == "Stripe"
    assert candidates[1].company_domain is None
    assert page.total_count == 12
    assert page.total_pages == 1
    assert page.free is False
    assert page.credits_used == 1

    args, kwargs = fake.post.call_args
    assert args[0] == "https://api.prospeo.io/search-person"
    assert kwargs["json"] == {
        "page": 1,
        "filters": {
            "company": {"names": {"include": ["Stripe"]}},
            "person_job_title": {"include": list(DEFAULT_RECRUITER_TITLES), "match_mode": "CONTAINS"},
            **VERIFY_ONLY_FILTER,
        },
    }
    assert kwargs["headers"]["X-KEY"] == KEY


def test_search_by_domain_uses_websites_filter(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, SEARCH_BODY)
    search_recruiters("stripe.com", http_client=fake)
    filters = fake.post.call_args.kwargs["json"]["filters"]
    assert filters["company"] == {"websites": {"include": ["stripe.com"]}}


def test_search_keyword_forwarded(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, SEARCH_BODY)
    search_recruiters("Stripe", keyword="engineering manager", http_client=fake)
    filters = fake.post.call_args.kwargs["json"]["filters"]
    assert filters["person_job_title"]["include"] == ["engineering manager"]


def test_search_free_repeat_is_zero_credits(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, dict(SEARCH_BODY, free=True))
    page = search_recruiters("Stripe", http_client=fake)
    assert page.free is True
    assert page.credits_used == 0


# ==============================================================================
# search_recruiters: error contract
# ==============================================================================

def test_search_no_results_is_free_and_actionable(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(400, {"error": True, "error_code": "NO_RESULTS"})
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_NO_RESULTS
    assert "recruiter profiles matched" in str(exc_info.value)
    assert "keyword" in str(exc_info.value)


def test_search_invalid_filters_reports_filter_error(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(
        400, {"error": True, "error_code": "INVALID_FILTERS", "filter_error": "person_job_title: too many include items"}
    )
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_INVALID_FILTERS
    assert "too many include items" in str(exc_info.value)
    assert exc_info.value.filter_error == "person_job_title: too many include items"


def test_search_insufficient_credits_tells_user_to_swap_key(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(400, {"error": True, "error_code": "INSUFFICIENT_CREDITS"})
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_INSUFFICIENT_CREDITS
    assert ".env" in str(exc_info.value)


def test_search_invalid_api_key_message(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(400, {"error": True, "error_code": "INVALID_API_KEY"})
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_INVALID_API_KEY
    assert "PROSPEO_API_KEY" in str(exc_info.value)


def test_search_rate_limited_reports_headers(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    resp = _response(429, {"error_code": "Rate limit exceeded"})
    resp.headers = {"x-daily-request-left": "5", "x-minute-reset-seconds": "20"}
    fake = MagicMock()
    fake.post.return_value = resp
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_RATE_LIMITED
    assert "5 search requests left today" in str(exc_info.value)
    assert "20" in str(exc_info.value)


def test_search_non_json_body(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(502, {})
    fake.post.return_value.json.side_effect = ValueError("bad json")
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_BAD_RESPONSE


def test_search_network_error_wrapped(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.side_effect = httpx.ConnectError("no network")
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_NETWORK


def test_search_requires_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", "")
    with pytest.raises(ValueError, match="PROSPEO_API_KEY is missing"):
        search_recruiters("Stripe")


# ==============================================================================
# Verified-email filter fallback (PLAN_REQUIRED / INVALID_FILTERS retry)
# ==============================================================================

def test_search_retries_without_verified_filter_when_plan_blocked(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    blocked = _response(
        400, {"error": True, "error_code": "PLAN_REQUIRED", "filter_error": "person_contact_details requires a paid plan"}
    )
    ok = _response(200, SEARCH_BODY)
    fake = MagicMock()
    fake.post.side_effect = [blocked, ok]

    page = search_recruiters("Stripe", http_client=fake)

    assert page.verified_only_effective is False
    assert page.credits_used == 1
    assert fake.post.call_count == 2
    first_filters = fake.post.call_args_list[0].kwargs["json"]["filters"]
    second_filters = fake.post.call_args_list[1].kwargs["json"]["filters"]
    assert "person_contact_details" in first_filters
    assert "person_contact_details" not in second_filters


def test_search_plan_required_unrelated_filter_is_not_retried(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(
        400, {"error": True, "error_code": "PLAN_REQUIRED", "filter_error": "company requires a Starter plan"}
    )
    with pytest.raises(ProspeoError) as exc_info:
        search_recruiters("Stripe", http_client=fake)
    assert exc_info.value.code == CODE_PLAN_REQUIRED
    assert fake.post.call_count == 1


# ==============================================================================
# enrich_person generalization (person_id path for search candidates)
# ==============================================================================

def test_enrich_person_by_id_request_and_parse(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, ENRICH_BODY)

    enriched = enrich_person(person_id="p1", http_client=fake)

    assert enriched.email == "jane.doe@stripe.com"
    assert enriched.person_id == "p1"
    assert enriched.linkedin_url == "https://www.linkedin.com/in/janedoe"
    assert enriched.credits_used == 1
    args, kwargs = fake.post.call_args
    assert args[0] == "https://api.prospeo.io/enrich-person"
    assert kwargs["json"] == {
        "only_verified_email": True,
        "enrich_mobile": False,
        "data": {"person_id": "p1"},
    }


def test_enrich_person_accepts_email_and_name_hints(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, ENRICH_BODY)
    enrich_person(email="jane.doe@stripe.com", full_name="Jane Doe", http_client=fake)
    assert fake.post.call_args.kwargs["json"]["data"] == {
        "email": "jane.doe@stripe.com",
        "full_name": "Jane Doe",
    }


def test_enrich_person_requires_a_datapoint():
    with pytest.raises(ValueError, match="at least one datapoint"):
        enrich_person(api_key=KEY)


def test_enrich_person_rejects_non_linkedin_urls():
    with pytest.raises(ValueError, match="LinkedIn"):
        enrich_person(linkedin_url="https://twitter.com/jane", api_key=KEY)


def test_enrich_prospect_prefers_person_id(monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_API_KEY", KEY)
    fake = MagicMock()
    fake.post.return_value = _response(200, ENRICH_BODY)

    candidate = ProspectCandidate(
        person_id="p1", full_name="Jane Doe", linkedin_url="https://www.linkedin.com/in/janedoe"
    )
    enrich_prospect(candidate, http_client=fake)
    assert "person_id" in fake.post.call_args.kwargs["json"]["data"]
    assert "linkedin_url" not in fake.post.call_args.kwargs["json"]["data"]

    url_only = ProspectCandidate(full_name="John TA", linkedin_url="https://www.linkedin.com/in/johnta")
    enrich_prospect(url_only, http_client=fake)
    assert "linkedin_url" in fake.post.call_args_list[1].kwargs["json"]["data"]


def test_enrich_prospect_rejects_empty_candidate():
    with pytest.raises(ValueError, match="neither a person_id nor a LinkedIn URL"):
        enrich_prospect(ProspectCandidate(full_name="Unknown"), api_key=KEY)


# ==============================================================================
# Probe budget ledger (rolling 24h window over the scheduler log)
# ==============================================================================

def test_prospeo_search_budget_counts_attempts(session: Session, monkeypatch):
    monkeypatch.setattr(settings, "PROSPEO_MAX_SEARCHES_PER_DAY", 1)
    from src.interfaces.bot import _prospeo_search_budget, _record_search_attempt

    _record_search_attempt(session, "Stripe|", outcome="ok")
    used, cap = _prospeo_search_budget(session)
    assert (used, cap) == (1, 1)
    _record_search_attempt(session, "Stripe|engineer", outcome="err:NO_RESULTS")
    used, _cap = _prospeo_search_budget(session)
    assert used == 2  # failed searches count too (conservative budget)
