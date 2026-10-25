"""Tests for Web Scraper Service (Offline & Mocked)."""

from unittest.mock import MagicMock, patch

import httpx

from src.services.scraper import scrape_url

HTML_SAMPLE = """
<!DOCTYPE html>
<html>
<head><title>Careers at Acme Corp</title></head>
<body>
    <nav><a href="/">Home</a><a href="/jobs">Jobs</a></nav>
    <main>
        <h1>Software Engineering Intern</h1>
        <p>Acme Corp is hiring software engineering interns for Summer 2026 in Bangalore.</p>
        <p>Eligible batches: Class of 2027 and 2028. Required skills: Python, SQL, REST APIs.</p>
        <p>Application deadline is December 15, 2026.</p>
    </main>
    <footer>Copyright 2026 Acme Corp. All rights reserved.</footer>
</body>
</html>
"""


def test_scrape_url_success():
    """Test successful scraping and text extraction using mocked HTTPX."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = HTML_SAMPLE
    mock_response.url = "https://careers.acme.com/jobs/123"

    with patch("httpx.Client.get", return_value=mock_response):
        result = scrape_url("https://careers.acme.com/jobs/123")

        assert result.success is True
        assert result.status_code == 200
        assert "Software Engineering Intern" in result.text
        assert "Python" in result.text


def test_scrape_url_http_404_error():
    """Test handling of 404 Not Found error (search fallback stubbed to stay offline)."""
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Page Not Found"
    mock_response.url = "https://careers.acme.com/jobs/404"

    with patch("httpx.Client.get", return_value=mock_response), patch(
        "src.services.scraper._search_grounding_fallback", return_value=None
    ):
        result = scrape_url("https://careers.acme.com/jobs/404")

        assert result.success is False
        assert result.status_code == 404
        assert "HTTP 404" in result.error_message


def test_scrape_url_timeout():
    """Test handling of network timeouts (search fallback stubbed to stay offline)."""
    with patch("httpx.Client.get", side_effect=httpx.TimeoutException("Connection timed out")), patch(
        "src.services.scraper._search_grounding_fallback", return_value=None
    ):
        result = scrape_url("https://slow-site.com/jobs")

        assert result.success is False
        assert "timed out" in result.error_message.lower()

def test_resolve_ashby_job_mocked():
    """Verify that Ashby ATS job resolution parses job board JSON accurately."""
    mock_ashby_payload = {
        "jobs": [
            {
                "id": "abc-123",
                "title": "Backend Engineering Intern",
                "department": "Infrastructure",
                "location": "Remote",
                "workplaceType": "Remote",
                "applyUrl": "https://jobs.ashbyhq.com/example/abc-123/apply",
                "descriptionPlain": "Develop high-throughput microservices using Python and PostgreSQL.",
            }
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_ashby_payload

    with patch("httpx.Client.get", return_value=mock_resp):
        res = scrape_url("https://example.com/careers?ashby_jid=abc-123")
        assert res.success is True
        assert res.status_code == 200
        assert "Backend Engineering Intern" in res.text
        assert "abc-123/apply" in res.final_url


# ---------------------------------------------------------------------------
# Optional headless-Chromium (Playwright) rendering path
# ---------------------------------------------------------------------------

PW_HTML = """
<!DOCTYPE html>
<html>
<head><title>Rivian Careers</title></head>
<body>
    <div id="root">
        <main><h1>Battery Systems Engineering Intern</h1>
        <p>Rivian is hiring a battery systems engineering intern for Summer 2026 in Irvine, California.</p>
        <p>Responsibilities include lithium-ion cell testing and thermal modeling. Deadline is January 31, 2027.</p></main>
    </div>
</body>
</html>
"""


def _fake_playwright(html: str, final_url: str) -> MagicMock:
    """Build a MagicMock chain mimicking sync_playwright()'s browser/context/page flow."""
    page = MagicMock()
    page.content.return_value = html
    page.url = final_url
    context = MagicMock()
    context.new_page.return_value = page
    browser = MagicMock()
    browser.new_context.return_value = context
    driver = MagicMock()
    driver.chromium.launch.return_value = browser
    module = MagicMock()
    module.return_value.__enter__.return_value = driver
    return module


def _patch_pw_available():
    """Patch both availability conditions (package installed + settings enabled)."""
    return patch("src.services.scraper._PLAYWRIGHT_INSTALLED", True), patch(
        "src.services.scraper.settings.PLAYWRIGHT_ENABLED", True
    )


def test_workday_uses_playwright_directly_when_available():
    """JS-only Workday portals must skip static HTTP and render in headless Chromium."""
    final_url = "https://rivian.wd5.myworkdayjobs.com/en-US/RivianExternal/job/BatteryIntern"
    pw_installed, pw_enabled = _patch_pw_available()
    with pw_installed, pw_enabled, patch(
        "src.services.scraper.sync_playwright", _fake_playwright(PW_HTML, final_url)
    ), patch("httpx.Client.get") as mock_get:
        res = scrape_url(final_url)

    assert res.success is True
    assert res.final_url == final_url
    assert "Battery Systems Engineering Intern" in res.text
    mock_get.assert_not_called()


def test_workday_without_playwright_falls_back_to_static_flow():
    """Without the optional dependency, Workday pages degrade to today's static behavior."""
    shell = "<html><body><div id='root'></div></body></html>"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = shell
    mock_response.url = "https://rivian.wd5.myworkdayjobs.com/en-US/Rivian/job/1"

    pw_installed, pw_enabled = _patch_pw_available()
    with patch("src.services.scraper._PLAYWRIGHT_INSTALLED", False), pw_enabled, patch(
        "httpx.Client.get", return_value=mock_response
    ), patch("src.services.scraper._search_grounding_fallback", return_value=None) as mock_search:
        res = scrape_url(mock_response.url)

    assert res.success is False
    mock_search.assert_called_once()


def test_empty_spa_shell_triggers_playwright_retry():
    """A 200 response containing only an SPA shell should be rendered and recovered."""
    shell = "<html><body><div id='root'></div></body></html>"
    final_url = "https://careers.acme.com/jobs/123"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = shell
    mock_response.url = final_url

    pw_installed, pw_enabled = _patch_pw_available()
    with pw_installed, pw_enabled, patch(
        "httpx.Client.get", return_value=mock_response
    ), patch("src.services.scraper.sync_playwright", _fake_playwright(PW_HTML, final_url)), patch(
        "src.services.scraper._search_grounding_fallback"
    ) as mock_search:
        res = scrape_url(final_url)

    assert res.success is True
    assert "Battery Systems Engineering Intern" in res.text
    mock_search.assert_not_called()


def test_bot_blocked_status_triggers_playwright_before_search_fallback():
    """HTTP 403 bot challenges should attempt a real browser render before search grounding."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.text = "Access Denied"
    mock_response.url = "https://careers.acme.com/jobs/777"

    pw_installed, pw_enabled = _patch_pw_available()
    with pw_installed, pw_enabled, patch(
        "httpx.Client.get", return_value=mock_response
    ), patch("src.services.scraper.sync_playwright", _fake_playwright(PW_HTML, mock_response.url)), patch(
        "src.services.scraper._search_grounding_fallback"
    ) as mock_search:
        res = scrape_url(mock_response.url)

    assert res.success is True
    mock_search.assert_not_called()


def test_playwright_crash_falls_back_to_search_grounding():
    """A headless render failure must swallow the exception and continue to search grounding."""
    shell = "<html><body><div id='root'></div></body></html>"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = shell
    mock_response.url = "https://careers.acme.com/jobs/999"

    pw_installed, pw_enabled = _patch_pw_available()
    failing = MagicMock()
    failing.side_effect = RuntimeError("browser launch failed")

    with pw_installed, pw_enabled, patch(
        "httpx.Client.get", return_value=mock_response
    ), patch("src.services.scraper.sync_playwright", failing), patch(
        "src.services.scraper._search_grounding_fallback", return_value=None
    ) as mock_search:
        res = scrape_url(mock_response.url)

    assert res.success is False
    mock_search.assert_called_once()
