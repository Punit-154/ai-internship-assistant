"""Tests for Opportunity Verification and Scam Detection (Milestone 5)."""

from unittest.mock import patch

from src.models.job import VerificationStatus
from src.services.verifier import (
    detect_scam_signals,
    verify_opportunity,
)


def test_detect_scam_signals_registration_fee():
    """Detect upfront registration or security deposit requests."""
    text = "Congratulations! Selected candidates must pay a registration fee of Rs. 1500 for training."
    signals = detect_scam_signals(text)
    assert len(signals) >= 1
    assert any("registration fee" in s.lower() or "deposit" in s.lower() for s in signals)


def test_detect_scam_signals_personal_email():
    """Detect non-corporate email addresses used for corporate applications."""
    text = "Send your resume and passport photo to hr.recruitment2026@gmail.com immediately."
    signals = detect_scam_signals(text)
    assert len(signals) >= 1
    assert any("personal email" in s.lower() for s in signals)


def test_detect_scam_signals_task_scam():
    """Detect fraudulent task/like scams."""
    text = "Earn 5000 daily! Like youtube videos and crypto task to complete daily quota."
    signals = detect_scam_signals(text)
    assert len(signals) >= 1


def test_verify_opportunity_trusted_ats():
    """Verify trusted ATS links are recognized as official."""
    res = verify_opportunity(
        company_name="Interview Kickstart",
        role_title="Backend Intern",
        url="https://jobs.ashbyhq.com/interview-kickstart/12345",
    )
    assert res.is_official is True
    assert res.verification_status == VerificationStatus.VERIFIED_OFFICIAL
    assert "ashbyhq.com" in res.notes


def test_verify_opportunity_scam_triggers_critical_warning():
    """Verify that detected scam signals flag the opportunity as SUSPICIOUS."""
    res = verify_opportunity(
        company_name="FakeTech Corp",
        role_title="Data Entry Intern",
        url="https://faketech.example.com",
        raw_text="Selected candidates must deposit Rs. 2000 processing charges before offer letter.",
    )
    assert res.is_official is False
    assert res.verification_status == VerificationStatus.SUSPICIOUS
    assert len(res.red_flags) >= 1


@patch("src.services.verifier.search_official_career_portal")
def test_verify_opportunity_search_grounding_discovery(mock_search):
    """Verify search grounding finds official career portal for unverified/aggregator links."""
    mock_search.return_value = "https://jobs.ashbyhq.com/company/real-role"
    res = verify_opportunity(
        company_name="Acme Corp",
        role_title="Software Intern",
        url="https://unstop.com/internships/acme-software-intern-123",
    )
    assert res.is_official is True
    assert res.verification_status == VerificationStatus.VERIFIED_OFFICIAL
    assert res.official_portal_url == "https://jobs.ashbyhq.com/company/real-role"


@patch("src.services.verifier.search_official_career_portal", return_value=None)
def test_verify_spoofed_ats_suffix_rejected(mock_search):
    """Regression: 'evilgreenhouse.io' must NOT verify as real 'greenhouse.io'."""
    res = verify_opportunity(
        company_name="Greenhouse",
        role_title="SWE Intern",
        url="https://evilgreenhouse.io/apply/123",
    )
    assert res.is_official is False
    assert res.verification_status == VerificationStatus.UNVERIFIED_THIRD_PARTY


@patch("src.services.verifier.search_official_career_portal", return_value=None)
def test_verify_company_substring_hostname_rejected(mock_search):
    """Regression: 'noamazon.com' must NOT verify as company domain for 'Amazon'."""
    res = verify_opportunity(
        company_name="Amazon",
        role_title="SWE Intern",
        url="https://noamazon.com/careers/998877",
    )
    assert res.is_official is False
    assert res.verification_status == VerificationStatus.UNVERIFIED_THIRD_PARTY


def test_verify_real_ats_subdomains_accepted():
    """Real ATS subdomains and company career hosts must still verify."""
    res_gh = verify_opportunity(
        company_name="Figma",
        role_title="SWE Intern",
        url="https://boards.greenhouse.io/figma/jobs/12345",
    )
    assert res_gh.is_official is True

    res_amazon = verify_opportunity(
        company_name="Amazon",
        role_title="SWE Intern",
        url="https://jobs.amazon.com/jobs/998877",
    )
    assert res_amazon.is_official is True


def test_verify_scam_url_not_returned_as_official_portal():
    """Regression: a SUSPICIOUS verdict must never propagate the scam URL as official_portal_url."""
    res = verify_opportunity(
        company_name="FakeTech Corp",
        role_title="Data Entry Intern",
        url="https://faketech.example.com",
        raw_text="Pay registration fee of Rs. 500 to apply.",
    )
    assert res.verification_status == VerificationStatus.SUSPICIOUS
    assert res.official_portal_url is None
