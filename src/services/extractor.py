"""Structured Job Information Extractor.

Uses Google Gemini Flash with Pydantic JSON schema enforcement to parse
unstructured job postings from text and multimodal screenshots/images into validated data models.
"""

import json
import logging
import mimetypes
from datetime import date, datetime
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
from src.config import settings

logger = logging.getLogger(__name__)


class ExtractedJobData(BaseModel):
    """Schema enforced on the LLM output for structured job extraction."""
    company_name: str = Field(description="Name of the hiring company or organization")
    role_title: str = Field(description="Exact or normalized job title (e.g., 'Software Engineering Intern')")
    locations: List[str] = Field(default_factory=list, description="List of cities/locations or 'Remote'/'Hybrid'")
    application_deadline: Optional[str] = Field(default=None, description="Application deadline in YYYY-MM-DD format if explicitly stated, otherwise null")
    application_url: Optional[str] = Field(default=None, description="Direct link or careers portal link if present in text or image, otherwise null")
    allowed_grad_years: List[int] = Field(default_factory=list, description="Target graduation years explicitly mentioned (e.g., [2027, 2028]). Leave empty if not specified.")
    is_internship: bool = Field(default=True, description="True if this is an internship, trainee, co-op, or student opportunity; False if full-time experienced role")
    required_skills: List[str] = Field(default_factory=list, description="Mandatory technical skills, languages, or tools mentioned")
    preferred_skills: List[str] = Field(default_factory=list, description="Good-to-have or preferred technical qualifications")
    eligibility_notes: str = Field(description="Summary of degree, major, CGPA, or batch constraints explicitly stated")
    confidence_score: float = Field(ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0 based on how complete the posting is")


EXTRACTION_SYSTEM_INSTRUCTION = """
You are a senior technical recruiter and extraction engine for student internship opportunities.
Your task is to extract structured internship data from raw text or visual documents (screenshots, flyers, PDFs) with absolute fidelity and zero fabrication.

CRITICAL RULES:
1. TRUTHFULNESS & GROUNDING: Only extract information explicitly visible in the input.
   - Do NOT invent companies, deadlines, locations, or skills.
   - If a field (like deadline or application URL) is not stated or visible, return null.
2. GRADUATION YEAR & ELIGIBILITY:
   - Identify any graduation year constraints (e.g., '2027, 2028', 'Class of 2028').
   - If stated by college year (e.g. '2nd year', '3rd year students', 'pre-final year'), map them based on a standard 4-year degree (Current reference year: 2026):
     * 1st year = 2029
     * 2nd year = 2028
     * 3rd year / Pre-final = 2027
     * 4th year / Final = 2026
   - If no year or batch is restricted, leave allowed_grad_years as an empty list [].
3. ROLE TYPE:
   - Set is_internship = False if the posting is explicitly for senior, staff, or experienced professionals (e.g., '5+ years experience required').
4. VISUAL & QR EXTRACTION:
   - If processing an image or flyer, carefully inspect for text overlays, captions, Bit.ly/URL links, or contact emails.
5. CLEAN SKILLS:
   - Extract discrete, standardized technical skills (e.g., 'Python', 'FastAPI', 'PostgreSQL', 'Docker', 'AWS', 'C++', 'Data Structures').
"""



FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"]


def generate_content_with_fallback(
    client: genai.Client,
    contents,
    config: types.GenerateContentConfig,
    model: Optional[str] = None,
):
    """Call Gemini generate_content with automatic fallback if quota 429 or availability error occurs."""
    primary = model or settings.GEMINI_MODEL
    candidate_models = [primary] + [m for m in FALLBACK_MODELS if m != primary]

    last_exc = None
    for m in candidate_models:
        try:
            return client.models.generate_content(
                model=m,
                contents=contents,
                config=config,
            )
        except Exception as exc:
            err_str = str(exc)
            if "429" in err_str or "404" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                logger.warning(f"Model {m} hit quota/availability limit ({exc}). Attempting fallback...")
                last_exc = exc
                continue
            raise
    raise last_exc


def get_genai_client() -> genai.Client:
    """Initialize and return the Google GenAI Client."""
    api_key = settings.GEMINI_API_KEY
    if not api_key or api_key.startswith("AIzaSy_your_gemini"):
        raise ValueError(
            "GEMINI_API_KEY is missing or invalid in your .env file.\n"
            "Please obtain a free key from https://aistudio.google.com/ and set it in .env"
        )
    return genai.Client(api_key=api_key)


def extract_job_from_text(
    text: str,
    source_url: Optional[str] = None,
    client: Optional[genai.Client] = None,
    model_name: Optional[str] = None,
) -> ExtractedJobData:
    """Extract structured internship data from raw text using Gemini Flash."""
    if not text or len(text.strip()) < 15:
        raise ValueError("Input text is too short to contain valid job information.")

    ai_client = client or get_genai_client()
    target_model = model_name or settings.GEMINI_MODEL

    prompt = f"Extract the internship opportunity details from the following posting:\n\n{text}"
    if source_url:
        prompt += f"\n\nSource URL: {source_url}"

    config = types.GenerateContentConfig(
        system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=ExtractedJobData,
        temperature=0.1,
    )

    try:
        response = generate_content_with_fallback(
            client=ai_client,
            contents=prompt,
            config=config,
            model=target_model,
        )

        raw_json = response.text
        if not raw_json:
            raise ValueError("LLM returned an empty response.")

        data_dict = json.loads(raw_json)
        return ExtractedJobData.model_validate(data_dict)

    except Exception as exc:
        logger.error(f"Failed to extract structured job details with Gemini: {exc}")
        raise


def extract_job_from_image(
    image_bytes: bytes,
    mime_type: str,
    client: Optional[genai.Client] = None,
    model_name: Optional[str] = None,
) -> ExtractedJobData:
    """Extract structured internship data directly from an image or PDF screenshot using Gemini Multimodal vision."""
    if not image_bytes or len(image_bytes) < 20:
        raise ValueError("Image data is empty or corrupt.")

    ai_client = client or get_genai_client()
    target_model = model_name or settings.GEMINI_MODEL

    image_part = types.Part.from_bytes(
        data=image_bytes,
        mime_type=mime_type,
    )

    prompt = (
        "Inspect this image / document thoroughly. Extract all visible internship announcement "
        "and hiring details into the structured schema. If an application URL, email, or link is "
        "visible, extract it. If no application link is present, return null."
    )

    config = types.GenerateContentConfig(
        system_instruction=EXTRACTION_SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        response_schema=ExtractedJobData,
        temperature=0.1,
    )

    try:
        response = generate_content_with_fallback(
            client=ai_client,
            contents=[image_part, prompt],
            config=config,
            model=target_model,
        )

        raw_json = response.text
        if not raw_json:
            raise ValueError("Multimodal LLM returned an empty response.")

        data_dict = json.loads(raw_json)
        return ExtractedJobData.model_validate(data_dict)

    except Exception as exc:
        logger.error(f"Failed multimodal extraction with Gemini: {exc}")
        raise
