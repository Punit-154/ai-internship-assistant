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
