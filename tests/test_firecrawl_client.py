from unittest.mock import Mock

import pytest

from tender_scraper.firecrawl_client import (
    ConfigurationError,
    FirecrawlAPIError,
    FirecrawlClient,
    RequestValidationError,
    build_crawl_payload,
    resolve_redirect_destination,
    unwrap_safelink,
    validate_target_url,
)


def test_build_payload_applies_defaults(monkeypatch):
    monkeypatch.delenv("DEFAULT_CRAWL_LIMIT", raising=False)
    monkeypatch.delenv("MAX_CRAWL_LIMIT", raising=False)

    payload = build_crawl_payload({"url": " https://example.com/docs "})

    assert payload["url"] == "https://example.com/docs"
    assert payload["limit"] == 1
    assert payload["scrapeOptions"]["onlyMainContent"] is True
    assert payload["scrapeOptions"]["maxAge"] == 0
    formats = payload["scrapeOptions"]["formats"]
    assert formats[:2] == ["links", "rawHtml"]
    assert formats[2]["type"] == "json"
    assert formats[2]["schema"]["required"] == [
        "name",
        "agency",
        "tenderID",
        "category",
        "closeDate",
        "locations",
        "type",
        "description",
        "contact",
        "url",
    ]


def test_build_payload_preserves_firecrawl_options(monkeypatch):
    monkeypatch.setenv("MAX_CRAWL_LIMIT", "500")
    body = {
        "url": "https://example.com",
        "limit": 20,
        "includePaths": ["/docs/*"],
        "scrapeOptions": {"formats": ["html"]},
    }

    payload = build_crawl_payload(body)
    assert payload["url"] == body["url"]
    assert payload["limit"] == 20
    assert payload["includePaths"] == ["/docs/*"]
    formats = payload["scrapeOptions"]["formats"]
    assert formats[:3] == ["html", "links", "rawHtml"]
    assert formats[3]["type"] == "json"
    assert payload["scrapeOptions"]["maxAge"] == 0


def test_build_payload_keeps_caller_cache_age():
    payload = build_crawl_payload(
        {"url": "https://example.com", "scrapeOptions": {"maxAge": 60000}}
    )

    assert payload["scrapeOptions"]["maxAge"] == 60000


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com",
        "file:///etc/passwd",
        "https://user:pass@example.com",
        "http://[invalid-ipv6",
    ],
)
def test_rejects_invalid_target_urls(url):
    with pytest.raises(RequestValidationError):
        validate_target_url(url)


def test_rejects_limit_over_configured_cap(monkeypatch):
    monkeypatch.setenv("DEFAULT_CRAWL_LIMIT", "10")
    monkeypatch.setenv("MAX_CRAWL_LIMIT", "25")
    with pytest.raises(RequestValidationError, match="between 1 and 25"):
        build_crawl_payload({"url": "https://example.com", "limit": 26})


def test_cloud_requires_api_key(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="API_KEY"):
        FirecrawlClient.from_environment()


def test_self_hosted_server_can_be_keyless(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.internal:3002")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)

    client = FirecrawlClient.from_environment()

    assert client.api_root == "http://firecrawl.internal:3002/v2"
    assert "Authorization" not in client.headers


def test_start_crawl_sends_bearer_token():
    response = Mock()
    response.ok = True
    response.json.return_value = {"success": True, "id": "job-123"}
    session = Mock()
    session.request.return_value = response
    client = FirecrawlClient(
        api_root="https://api.firecrawl.dev/v2",
        api_key="secret",
        session=session,
    )

    result = client.start_crawl({"url": "https://example.com", "limit": 1})

    assert result["id"] == "job-123"
    session.request.assert_called_once_with(
        "POST",
        "https://api.firecrawl.dev/v2/crawl",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "tender-scraper/1.0",
            "Authorization": "Bearer secret",
        },
        timeout=30,
        json={"url": "https://example.com", "limit": 1},
    )


def test_upstream_error_is_exposed_without_secrets():
    response = Mock()
    response.ok = False
    response.status_code = 429
    response.json.return_value = {"error": "rate limited"}
    response.headers = {"Retry-After": "10"}
    session = Mock()
    session.request.return_value = response
    client = FirecrawlClient("https://api.firecrawl.dev/v2", "secret", session=session)

    with pytest.raises(FirecrawlAPIError) as caught:
        client.start_crawl({"url": "https://example.com"})

    assert caught.value.status_code == 429
    assert caught.value.retry_after == "10"
    assert "secret" not in str(caught.value.payload)


def test_unwraps_outlook_safelink():
    protected_url = (
        "https://apc01.safelinks.protection.outlook.com/"
        "?url=https%3A%2F%2Fconnect.buy.nsw.gov.au%2Flink%2Fabc%2Fpage.html"
        "&amp;data=ignored"
    )

    assert unwrap_safelink(protected_url) == (
        "https://connect.buy.nsw.gov.au/link/abc/page.html"
    )


def test_resolves_tracking_url_location_header():
    response = Mock()
    response.headers = {
        "Location": "https://buy.nsw.gov.au/prcOpportunity/opportunity-123"
    }
    response.raise_for_status.return_value = None
    session = Mock()
    session.get.return_value = response

    resolved = resolve_redirect_destination(
        "https://connect.buy.nsw.gov.au/link/abc/page.html",
        session=session,
    )

    assert resolved == "https://buy.nsw.gov.au/prcOpportunity/opportunity-123"
    session.get.assert_called_once_with(
        "https://connect.buy.nsw.gov.au/link/abc/page.html",
        allow_redirects=False,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    response.close.assert_called_once()


def test_direct_url_does_not_make_resolution_request():
    session = Mock()

    resolved = resolve_redirect_destination(
        "https://buy.nsw.gov.au/prcOpportunity/opportunity-123",
        session=session,
    )

    assert resolved == "https://buy.nsw.gov.au/prcOpportunity/opportunity-123"
    session.get.assert_not_called()
