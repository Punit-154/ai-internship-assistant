"""Services package exports."""

from src.services.extractor import (
    ExtractedJobData,
    extract_job_from_image,
    extract_job_from_text,
)
from src.services.pipeline import (
    evaluate_batch_eligibility,
    process_image_pipeline,
    process_text_pipeline,
    process_url_pipeline,
    verify_portal_authenticity,
)
from src.services.profile import (
    MatchEvaluation,
    add_project_to_profile,
    evaluate_job_match,
    get_candidate_profile,
    parse_project_from_text,
    save_candidate_profile,
    update_candidate_profile,
)
from src.services.resume import (
    TailoredResumeResult,
    render_resume_markdown,
    tailor_resume_for_job,
)
from src.services.scraper import ScrapeResult, scrape_url
from src.services.verifier import (
    TRUSTED_ATS_DOMAINS,
    VerificationResult,
    detect_scam_signals,
    search_official_career_portal,
    verify_opportunity,
)

__all__ = [
    "ExtractedJobData",
    "MatchEvaluation",
    "ScrapeResult",
    "TRUSTED_ATS_DOMAINS",
    "TailoredResumeResult",
    "VerificationResult",
    "add_project_to_profile",
    "detect_scam_signals",
    "evaluate_batch_eligibility",
    "evaluate_job_match",
    "extract_job_from_image",
    "extract_job_from_text",
    "get_candidate_profile",
    "parse_project_from_text",
    "process_image_pipeline",
    "process_text_pipeline",
    "process_url_pipeline",
    "render_resume_markdown",
    "save_candidate_profile",
    "scrape_url",
    "search_official_career_portal",
    "tailor_resume_for_job",
    "update_candidate_profile",
    "verify_opportunity",
    "verify_portal_authenticity",
]
