"""Web Scraper Service.

Fetches web pages using HTTPX with realistic headers and extracts the main
text content using Trafilatura, stripping out navigation, ads, and boilerplate.
Includes specialized ATS resolution (Ashby, Greenhouse, Lever) and DuckDuckGo
search-grounding fallback when career pages are protected by WAFs, Cloudflare, or JS SPAs.
"""

import logging
import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional, List
import httpx
import trafilatura
from ddgs import DDGS

logger = logging.getLogger(__name__)

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
        parts = domain.split(".")
        name = parts[0] if parts[0] not in {"www", "careers", "jobs"} else parts[1]
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
            logger.debug(f"Ashby check failed for slug {slug}: {exc}")

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
        parts = domain.split(".")
        board_token = parts[0] if parts[0] not in {"www", "careers", "jobs"} else parts[1]

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
        logger.debug(f"Greenhouse check failed: {exc}")

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
        logger.debug(f"Lever check failed: {exc}")

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
            if any(ats in href for ats in ["ashbyhq.com", "greenhouse.io", "lever.co", "myworkdayjobs.com"]):
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
    2. Direct HTTP request with realistic browser headers & Trafilatura text extraction.
    3. Search grounding fallback via DuckDuckGo if blocked by Cloudflare/WAF (403/401/SPA).
    
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

    # 2. Standard Web Scraping with HTTPX
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
                html_content = response.text
                extracted_text = trafilatura.extract(
                    html_content,
                    url=final_url,
                    include_links=True,
                    include_comments=False,
                    output_format="txt",
                    deduplicate=True,
                )

                if not extracted_text or len(extracted_text.strip()) < 50:
                    extracted_text = trafilatura.baseline(html_content)[1] if html_content else ""

                if extracted_text and len(extracted_text.strip()) >= 50:
                    return ScrapeResult(
                        url=url,
                        final_url=final_url,
                        success=True,
                        status_code=status_code,
                        text=extracted_text.strip(),
                    )
            
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
