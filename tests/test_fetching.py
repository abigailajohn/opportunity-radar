from __future__ import annotations

from datetime import date

import httpx
import pytest

from opportunity_radar.fetching import (
    FetchConnectionError,
    FetchHttpStatusError,
    FetchTimeoutError,
    HttpPageFetcher,
    InvalidResponseBodyError,
    ResponseTooLargeError,
    UnsupportedContentTypeError,
)
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.models import EligibilityRequirements
from opportunity_radar.pipeline import run_pipeline
from opportunity_radar.providers import FakeOpportunityExtractor, FakeSemanticAssessor
from conftest import make_judgment, make_opportunity


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normal_html_fetch_and_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("OpportunityRadar/")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8", "X-Test": "yes"},
            content=b"<html><head><title> Opportunity </title></head><body><h1>Apply</h1></body></html>",
            request=request,
        )

    with _client(handler) as client:
        page = HttpPageFetcher(client=client).fetch("https://example.test/opportunity")
    assert page.requested_url == "https://example.test/opportunity"
    assert page.final_url == "https://example.test/opportunity"
    assert page.status_code == 200
    assert page.content_type == "text/html"
    assert page.page_title == "Opportunity"
    assert page.byte_count == len(page.raw_html.encode("utf-8"))
    assert "Apply" in page.cleaned_text
    assert page.response_headers["x-test"] == "yes"


def test_redirect_records_final_url_and_history() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"}, request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<title>Final</title><p>Content</p>",
            request=request,
        )

    with _client(handler) as client:
        page = HttpPageFetcher(client=client).fetch("https://example.test/start")
    assert page.final_url == "https://example.test/final"
    assert [(hop.url, hop.status_code) for hop in page.redirect_history] == [
        ("https://example.test/start", 302)
    ]


@pytest.mark.parametrize("status", [404, 500])
def test_http_error_status(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

    with _client(handler) as client, pytest.raises(FetchHttpStatusError) as exc_info:
        HttpPageFetcher(client=client).fetch("https://example.test/missing")
    assert exc_info.value.status_code == status


def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with _client(handler) as client, pytest.raises(FetchTimeoutError):
        HttpPageFetcher(client=client).fetch("https://example.test/slow")


def test_connection_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("DNS failed", request=request)

    with _client(handler) as client, pytest.raises(FetchConnectionError, match="DNS failed"):
        HttpPageFetcher(client=client).fetch("https://not-found.test/")


@pytest.mark.parametrize("content_type", ["application/pdf", "application/json", ""])
def test_unsupported_content_type(content_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Type": content_type} if content_type else {}
        return httpx.Response(200, headers=headers, content=b"document", request=request)

    with _client(handler) as client, pytest.raises(UnsupportedContentTypeError):
        HttpPageFetcher(client=client).fetch("https://example.test/document")


def test_response_size_limit_from_streamed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"<p>far too large</p>",
            request=request,
        )

    with _client(handler) as client, pytest.raises(ResponseTooLargeError):
        HttpPageFetcher(client=client, maximum_bytes=8).fetch("https://example.test/large")


def test_empty_html_body_is_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=b"",
            request=request,
        )

    with _client(handler) as client, pytest.raises(InvalidResponseBodyError):
        HttpPageFetcher(client=client).fetch("https://example.test/empty")


def test_html_cleaning_removes_noise_and_preserves_structure_and_links() -> None:
    html = """
    <html><body>
      <nav>Menu item</nav><script>secret()</script><style>.x { color: red; }</style>
      <noscript>Enable scripts</noscript>
      <main><h1> Fellowship  2027 </h1><p> Apply   for the programme. </p>
      <ul><li>Travel funded</li><li><a href="/apply">Apply now</a></li></ul></main>
      <footer>Copyright</footer>
    </body></html>
    """
    cleaned = prepare_html(html, base_url="https://example.test/opportunity")
    assert "Menu item" not in cleaned
    assert "secret" not in cleaned
    assert "Enable scripts" not in cleaned
    assert "Copyright" not in cleaned
    assert "Fellowship 2027" in cleaned
    assert "- Travel funded" in cleaned
    assert "Apply now (https://example.test/apply)" in cleaned
    assert "  " not in cleaned


def test_real_fetcher_batch_isolation_with_mock_transport(profile) -> None:
    good_url = "https://example.test/good"
    bad_url = "https://example.test/bad"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bad":
            return httpx.Response(500, request=request)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text="<title>Good</title><p>Opportunity</p>",
            request=request,
        )

    opportunity = make_opportunity(
        url=good_url,
        eligibility=EligibilityRequirements(requirements_complete=True, student_required=True),
    )
    with _client(handler) as client:
        result = run_pipeline(
            [bad_url, good_url],
            profile,
            HttpPageFetcher(client=client),
            FakeOpportunityExtractor({good_url: opportunity}),
            FakeSemanticAssessor({str(opportunity.source_url): make_judgment()}),
            as_of=date(2026, 8, 27),
        )
    assert result.fetched_count == 1
    assert len(result.assessments) == 1
    assert len(result.failures) == 1
    assert result.failures[0].stage.value == "fetch"
    assert "500" in result.failures[0].reason
