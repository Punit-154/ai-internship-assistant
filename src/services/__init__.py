"""Services package exports."""

from src.services.alumni_finder import AlumniFinderError, find_engineers_on_github
from src.services.contact_enrichment import (
    EnrichedContact,
    ProspeoError,
    enrich_linkedin_profile,
    enrichment_available,
    validate_prospero_api_key,
)
from src.services.extractor import (
    ExtractedJobData,
    extract_job_from_image,
    extract_job_from_text,
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
    rebuild_tailored_resume,
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
    "AlumniFinderError",
    "EnrichedContact",
    "ExtractedJobData",
    "MatchEvaluation",
    "OutreachDraft",
    "OutreachError",
    "ProspeoError",
    "ScrapeResult",
    "TRUSTED_ATS_DOMAINS",
    "TailoredResumeResult",
    "VerificationResult",
    "add_project_to_profile",
    "approve_and_send",
    "craft_outreach_draft",
    "detect_scam_signals",
    "enrich_linkedin_profile",
    "enrichment_available",
    "evaluate_batch_eligibility",
    "evaluate_job_match",
    "extract_job_from_image",
    "extract_job_from_text",
    "find_engineers_on_github",
    "get_candidate_profile",
    "get_contacts_needing_followup",
    "mark_contact_replied",
    "parse_project_from_text",
    "process_image_pipeline",
    "process_text_pipeline",
    "process_url_pipeline",
    "rebuild_tailored_resume",
    "render_resume_markdown",
    "save_candidate_profile",
    "scrape_url",
    "search_official_career_portal",
    "tailor_resume_for_job",
    "update_candidate_profile",
    "upsert_contact",
    "validate_prospero_api_key",
    "verify_opportunity",
    "verify_portal_authenticity",
]
