"""Tests for Web Scraper Service (Offline & Mocked)."""

from unittest.mock import MagicMock, patch
import httpx
from src.services.scraper import ScrapeResult, scrape_url


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
