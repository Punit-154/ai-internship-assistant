"""Web Scraper Service.

Fetches web pages using HTTPX with realistic headers and extracts the main
text content using Trafilatura, stripping out navigation, ads, and boilerplate.
Includes specialized ATS resolution (Ashby, Greenhouse, Lever), an optional
headless-Chromium (Playwright) renderer for JavaScript-only ATS portals such
as Workday, and DuckDuckGo search-grounding fallback when career pages are
still protected by WAFs or Cloudflare bot challenges.
"""

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura
from ddgs import DDGS

from src.config import settings
from src.services.verifier import matched_trusted_ats

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_INSTALLED = True
except ImportError:
    sync_playwright = None
    _PLAYWRIGHT_INSTALLED = False

logger = logging.getLogger(__name__)

# Minimum extracted characters for a fetch to count as real content;
# anything smaller is treated as an empty client-side-rendered shell.
MIN_TEXT_LEN = 50

# Career portals whose content is rendered entirely client-side. Static HTTP
# requests always receive an empty HTML shell, so these jump straight to the
# headless renderer when one is available.
JS_HEAVY_ATS_DOMAINS = ("myworkdayjobs.com",)

# Resource types skipped during browser rendering to keep it fast and cheap.
_PW_BLOCKED_RESOURCES = frozenset({"image", "media", "font"})

# HTTP statuses that usually indicate a bot challenge worth retrying in a real browser.
_BOT_BLOCK_STATUSES = frozenset({401, 403, 429, 503})

# Standard modern browser headers to minimize bot blocking
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class ScrapeResult:
    """Result of scraping a target URL."""
    url: str
    final_url: str
    success: bool
    text: str = ""
    status_code: Optional[int] = None
    error_message: Optional[str] = None


def _is_js_heavy_ats(url: str) -> bool:
    """Check whether a URL points at a career portal whose content is rendered client-side only."""
    hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    return any(hostname == d or hostname.endswith("." + d) for d in JS_HEAVY_ATS_DOMAINS)


def _extract_clean_text(html_content: str, base_url: Optional[str] = None) -> Optional[str]:
    """Extract the main readable text from raw HTML, returning None below the content threshold."""
    if not html_content:
        return None
    extracted = trafilatura.extract(
        html_content,
        url=base_url,
        include_links=True,
        include_comments=False,
        output_format="txt",
        deduplicate=True,
    )
    if not extracted or len(extracted.strip()) < MIN_TEXT_LEN:
        extracted = trafilatura.baseline(html_content)[1]
    if extracted and len(extracted.strip()) >= MIN_TEXT_LEN:
        return extracted.strip()
    return None


def playwright_available() -> bool:
    """True when the optional playwright package is installed and not disabled via settings."""
    return _PLAYWRIGHT_INSTALLED and settings.PLAYWRIGHT_ENABLED


def _playwright_fetch(url: str, timeout: float = 12.0) -> Optional[ScrapeResult]:
    """Render a JavaScript-heavy page in headless Chromium and extract its main text.

    Images, media, and fonts are blocked to keep rendering fast; the extracted
    content is held to the same quality threshold as the static HTTP path so a
    rendered-but-empty shell is not mistaken for a successful fetch.
    """
    if sync_playwright is None:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                )

                def _block_heavy_resources(route):
                    if route.request.resource_type in _PW_BLOCKED_RESOURCES:
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", _block_heavy_resources)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
                html_content = page.content()
                final_url = page.url
                context.close()
            finally:
                browser.close()
        text = _extract_clean_text(html_content, final_url)
        if not text:
            logger.warning(f"Headless render produced no readable text for {url}")
            return None
        return ScrapeResult(url=url, final_url=final_url, success=True, status_code=200, text=text)
    except Exception as exc:
        logger.warning(f"Headless render failed for {url}: {exc}")
        return None


def _resolve_ashby_job(url: str, timeout: float = 8.0) -> Optional[ScrapeResult]:
    """Attempt direct API retrieval for Ashby-hosted job postings."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    ashby_id = qs.get("ashby_jid", [None])[0]

    slug_candidates = []
    if "ashbyhq.com" in parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            slug_candidates.append(parts[0])
            if not ashby_id and len(parts) > 1:
                ashby_id = parts[1]
    else:
        domain = parsed.netloc.lower()
        parts = [p for p in domain.split(".") if p]
        if not parts:
            return None
        skip_labels = {"www", "careers", "jobs", "com", "net", "org", "co", "in", "boards"}
        name = parts[0] if parts[0] not in skip_labels else next(
            (p for p in parts[1:] if p not in skip_labels), None
        )
        if not name:
            return None
        slug_candidates.append(name)
        if "-" not in name:
            for split_pt in range(4, len(name) - 3):
                slug_candidates.append(f"{name[:split_pt]}-{name[split_pt:]}")

    if not ashby_id:
        return None

    for slug in set(slug_candidates):
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(api_url)
                if resp.status_code == 200:
                    data = resp.json()
                    jobs = data.get("jobs", [])
                    for j in jobs:
                        if j.get("id") == ashby_id:
                            text_parts = [
                                f"Company Board: {slug}",
                                f"Role Title: {j.get('title', '')}",
                                f"Department: {j.get('department', '')}",
                                f"Location: {j.get('location', '')}",
                                f"Workplace Type: {j.get('workplaceType', '')}",
                                f"Employment Type: {j.get('employmentType', '')}",
                                f"Direct Apply URL: {j.get('applyUrl', '')}",
                                "\nJob Description:\n" + (j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
                            ]
                            return ScrapeResult(
                                url=url,
                                final_url=j.get("applyUrl") or j.get("jobUrl") or url,
                                success=True,
                                status_code=200,
                                text="\n".join(text_parts).strip(),
                            )
        except Exception as exc:
            logger.warning(f"Ashby check failed for slug {slug}: {exc}")

    return None


def _resolve_greenhouse_job(url: str, timeout: float = 8.0) -> Optional[ScrapeResult]:
    """Attempt direct API retrieval for Greenhouse job postings."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    gh_id = qs.get("gh_jid", [None])[0]

    board_token = None
    if "greenhouse.io" in parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            board_token = parts[0]
            if not gh_id and len(parts) > 2 and parts[1] == "jobs":
                gh_id = parts[2]
    else:
        domain = parsed.netloc.lower()
        parts = [p for p in domain.split(".") if p]
        if not parts:
            return None
        skip_labels = {"www", "careers", "jobs", "com", "net", "org", "co", "in", "boards"}
        board_token = parts[0] if parts[0] not in skip_labels else next(
            (p for p in parts[1:] if p not in skip_labels), None
        )
        if not board_token:
            return None

    if not gh_id or not board_token:
        return None

    api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{gh_id}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(api_url)
            if resp.status_code == 200:
                data = resp.json()
                content = trafilatura.extract(data.get("content", ""), output_format="txt") or data.get("content", "")
                text_parts = [
                    f"Company Board: {board_token}",
                    f"Role Title: {data.get('title', '')}",
                    f"Location: {data.get('location', {}).get('name', '')}",
                    f"Direct Apply URL: {data.get('absolute_url', '')}",
                    "\nJob Description:\n" + content,
                ]
                return ScrapeResult(
                    url=url,
                    final_url=data.get("absolute_url") or url,
                    success=True,
                    status_code=200,
                    text="\n".join(text_parts).strip(),
                )
    except Exception as exc:
        logger.warning(f"Greenhouse check failed: {exc}")

    return None


def _resolve_lever_job(url: str, timeout: float = 8.0) -> Optional[ScrapeResult]:
    """Attempt direct API retrieval for Lever job postings."""
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    lever_id = qs.get("lever_jid", [None])[0]

    company_slug = None
    if "lever.co" in parsed.netloc:
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            company_slug = parts[0]
            if not lever_id and len(parts) > 1:
                lever_id = parts[1]

    if not lever_id or not company_slug:
        return None

    api_url = f"https://api.lever.co/v0/postings/{company_slug}/{lever_id}"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(api_url)
            if resp.status_code == 200:
                data = resp.json()
                text_parts = [
                    f"Company: {company_slug}",
                    f"Role Title: {data.get('text', '')}",
                    f"Location: {data.get('categories', {}).get('location', '')}",
                    f"Workplace Type: {data.get('categories', {}).get('workplaceType', '')}",
                    f"Direct Apply URL: {data.get('applyUrl', '')}",
                    "\nJob Description:\n" + (data.get("descriptionPlain") or data.get("description") or ""),
                ]
                return ScrapeResult(
                    url=url,
                    final_url=data.get("applyUrl") or data.get("hostedUrl") or url,
                    success=True,
                    status_code=200,
                    text="\n".join(text_parts).strip(),
                )
    except Exception as exc:
        logger.warning(f"Lever check failed: {exc}")

    return None


def _search_grounding_fallback(url: str) -> Optional[ScrapeResult]:
    """Search DuckDuckGo when target URL is blocked by anti-bot/WAF (HTTP 403/401/SPA)."""
    parsed = urllib.parse.urlparse(url)
    domain = parsed.netloc.replace("www.", "").split(".")[0]
    
    query_parts = []
    qs = urllib.parse.parse_qs(parsed.query)
    for k, vals in qs.items():
        if "jid" in k.lower() or "id" in k.lower() or "role" in k.lower() or "title" in k.lower():
            query_parts.extend(vals)

    segments = [s for s in parsed.path.split("/") if s]
    for seg in reversed(segments):
        words = [
            w for w in re.split(r"[-_]", seg)
            if len(w) > 2 and not w.isdigit() and w.lower() not in {"jobs", "careers", "internships", "job", "career", "posting", "postings"}
        ]
        if words:
            query_parts.extend(words)
            break

    if not query_parts:
        path_words = [p for p in re.split(r"[/_-]", parsed.path) if len(p) > 2 and not p.isdigit()]
        query_parts.extend(path_words[:5])

    if domain not in {"jobs", "careers", "boards", "unstop", "linkedin", "internshala"}:
        query_parts.insert(0, domain)

    search_query = " ".join(dict.fromkeys(query_parts))
    logger.info(f"Initiating search grounding fallback for {url} with query: '{search_query}'")

    try:
        results = list(DDGS().text(search_query, max_results=5))
        if not results:
            return None

        # Build combined context from search results
        snippets = []
        best_url = url
        for item in results:
            title = item.get("title", "")
            href = item.get("href", "")
            body = item.get("body", "")
            snippets.append(f"Source: {href}\nTitle: {title}\nSummary: {body}\n")
            # Physical hostname check — not a raw substring test, so URLs like
            # 'https://scam.com/payload?greenhouse.io' don't get promoted.
            href_host = (urllib.parse.urlparse(href).hostname or "").lower()
            if matched_trusted_ats(href_host):
                best_url = href

        combined_text = (
            f"Note: Target URL '{url}' was protected by bot challenges or rendered via client-side JavaScript. "
            f"The following details were retrieved via search grounding:\n\n"
            + "\n".join(snippets)
        )
        return ScrapeResult(
            url=url,
            final_url=best_url,
            success=True,
            status_code=200,
            text=combined_text.strip(),
        )
    except Exception as exc:
        logger.warning(f"Search grounding fallback failed for {url}: {exc}")
        return None


def scrape_url(url: str, timeout: float = 12.0) -> ScrapeResult:
    """Fetch and extract the main body text from a webpage URL.
    
    Tries:
    1. Dedicated ATS API extraction (Ashby, Greenhouse, Lever).
    2. Straight headless-Chromium render for known JS-only portals (Workday).
    3. Direct HTTP request with realistic browser headers & Trafilatura text extraction.
    4. Headless-Chromium retry when HTTP returns an empty SPA shell or a bot-challenge status.
    5. Search grounding fallback via DuckDuckGo if the page is still inaccessible.
    
    Args:
        url: The web URL to fetch.
        timeout: Maximum seconds to wait for connection and response.
        
    Returns:
        ScrapeResult containing success status, extracted text, or error message.
    """
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # 1. Specialized ATS Resolvers
    ats_result = _resolve_ashby_job(url, timeout=timeout) or \
                 _resolve_greenhouse_job(url, timeout=timeout) or \
                 _resolve_lever_job(url, timeout=timeout)
    if ats_result:
        logger.info(f"Successfully resolved job via dedicated ATS provider for: {url}")
        return ats_result

    # 2. Known JS-only portals always answer static HTTP with an empty shell,
    #    so render them directly in headless Chromium when available.
    pw_attempted = False
    if _is_js_heavy_ats(url) and playwright_available():
        pw_attempted = True
        pw_result = _playwright_fetch(url, timeout=timeout)
        if pw_result:
            logger.info(f"Successfully rendered JS-heavy ATS page via headless Chromium: {url}")
            return pw_result

    # 3. Standard Web Scraping with HTTPX
    try:
        with httpx.Client(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            response = client.get(url)
            final_url = str(response.url)
            status_code = response.status_code

            if status_code < 400:
                extracted_text = _extract_clean_text(response.text, final_url)
                if extracted_text:
                    return ScrapeResult(
                        url=url,
                        final_url=final_url,
                        success=True,
                        status_code=status_code,
                        text=extracted_text,
                    )

            # 4. Static fetch came back empty or is blocking bots: a real browser
            #    render is the last resort before falling back to search grounding.
            if not pw_attempted and playwright_available() and (
                status_code < 400 or status_code in _BOT_BLOCK_STATUSES
            ):
                pw_result = _playwright_fetch(url, timeout=timeout)
                if pw_result:
                    logger.info(f"Recovered {url} via headless Chromium after static scrape failed (HTTP {status_code}).")
                    return pw_result

            # If status_code >= 400 or content was empty SPA/JS challenge
            logger.warning(f"HTTP {status_code} or empty text for URL: {url}. Attempting search fallback.")
            search_res = _search_grounding_fallback(url)
            if search_res:
                return search_res

            return ScrapeResult(
                url=url,
                final_url=final_url,
                success=False,
                status_code=status_code,
                error_message=f"HTTP {status_code}: Page not accessible or protected by Cloudflare bot challenge.",
            )

    except httpx.TimeoutException:
        logger.warning(f"Timeout while fetching URL: {url}. Trying search grounding fallback.")
        search_res = _search_grounding_fallback(url)
        if search_res:
            return search_res
        return ScrapeResult(
            url=url,
            final_url=url,
            success=False,
            error_message="Request timed out. The server took too long to respond.",
        )
    except httpx.RequestError as exc:
        logger.warning(f"Network error while fetching URL {url}: {exc}. Trying search grounding fallback.")
        search_res = _search_grounding_fallback(url)
        if search_res:
            return search_res
        return ScrapeResult(
            url=url,
            final_url=url,
            success=False,
            error_message=f"Network error: {str(exc)}",
        )
    except Exception as exc:
        logger.exception(f"Unexpected error while scraping {url}: {exc}")
        return ScrapeResult(
            url=url,
            final_url=url,
            success=False,
            error_message=f"Unexpected error: {str(exc)}",
        )
