from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import app as service

API_KEY = "test-api-key"
AUTH = {"x-api-key": API_KEY}


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    monkeypatch.setenv("API_KEYS", f"other-key, {API_KEY}")
    monkeypatch.delenv("API_AUTH_DISABLED", raising=False)


@pytest.fixture
def client():
    return TestClient(service.app, base_url="https://scraper.example.com")


@pytest.fixture(autouse=True)
def mock_save_artifacts(monkeypatch):
    saver = Mock(
        return_value={
            "status": "completed",
            "folder": "downloads/crawl-123",
            "summary": {"status": "saved", "fileName": "summary.json"},
            "html": {"status": "saved", "fileName": "website.html"},
        }
    )
    monkeypatch.setattr(service, "save_crawl_artifacts", saver)
    return saver


@pytest.fixture(autouse=True)
def mock_onedrive_upload(monkeypatch):
    uploader = Mock(
        return_value={
            "status": "completed",
            "folderWebUrl": "https://onedrive.example/folder",
            "total": 3,
            "uploaded": 3,
            "failed": 0,
            "files": [],
        }
    )
    monkeypatch.setattr(service, "upload_job_outputs", uploader)
    return uploader


@patch("app.resolve_redirect_destination")
@patch("app.download_attachments")
@patch("app.time")
@patch("app.FirecrawlClient.from_environment")
def test_start_handler_polls_until_complete_and_downloads(
    from_environment,
    time_module,
    download_attachments,
    resolve_redirect_destination,
    mock_save_artifacts,
    mock_onedrive_upload,
    client,
    monkeypatch,
):
    monkeypatch.delenv("DEFAULT_CRAWL_LIMIT", raising=False)
    monkeypatch.delenv("MAX_CRAWL_LIMIT", raising=False)
    time_module.monotonic.return_value = 0.0
    firecrawl = Mock()
    firecrawl.start_crawl.return_value = {
        "success": True,
        "id": "crawl-123",
        "url": "https://api.firecrawl.dev/v2/crawl/crawl-123",
    }
    firecrawl.get_crawl_status.side_effect = [
        {
            "success": True,
            "status": "scraping",
            "completed": 0,
            "total": 1,
            "data": [],
        },
        {
            "success": True,
            "status": "completed",
            "completed": 1,
            "total": 1,
            "data": [
                {
                    "links": ["https://example.com/files/specification.pdf"],
                    "rawHtml": "<html><h1>Tender title</h1></html>",
                    "json": {
                        "name": "Tender title",
                        "agency": "Example agency",
                        "tenderID": "RFT-1",
                    },
                    "metadata": {"sourceURL": "https://example.com/final"},
                }
            ],
        },
    ]
    download_attachments.return_value = {
        "status": "completed",
        "folder": "downloads/crawl-123",
        "total": 1,
        "downloaded": 1,
        "existing": 0,
        "failed": 0,
        "files": [],
    }
    from_environment.return_value = firecrawl
    resolve_redirect_destination.return_value = "https://example.com/final"

    response = client.post(
        "/api/crawl?trace=do-not-echo",
        headers=AUTH,
        json={"url": "https://example.com", "limit": 2},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["statusUrl"] == "https://scraper.example.com/api/crawl/crawl-123"
    assert "do-not-echo" not in response.text
    assert payload["status"] == "completed"
    assert payload["terminal"] is True
    assert payload["attachmentLinks"] == ["https://example.com/files/specification.pdf"]
    assert payload["downloads"]["downloaded"] == 1
    assert payload["polling"]["attempts"] == 2
    assert payload["resolvedUrl"] == "https://example.com/final"
    assert payload["summary"]["name"] == "Tender title"
    assert payload["summary"]["url"] == "https://example.com/final"
    submitted_payload = firecrawl.start_crawl.call_args.args[0]
    assert submitted_payload["url"] == "https://example.com/final"
    assert submitted_payload["limit"] == 2
    formats = submitted_payload["scrapeOptions"]["formats"]
    assert formats[:2] == ["links", "rawHtml"]
    assert formats[2]["type"] == "json"
    assert firecrawl.get_crawl_status.call_count == 2
    time_module.sleep.assert_called_once_with(5)
    download_attachments.assert_called_once_with(
        "crawl-123", ["https://example.com/files/specification.pdf"]
    )
    mock_save_artifacts.assert_called_once_with(
        "crawl-123",
        payload["summary"],
        "<html><h1>Tender title</h1></html>",
    )
    mock_onedrive_upload.assert_called_once_with(
        "crawl-123",
        payload["artifacts"],
        payload["downloads"],
        summary=payload["summary"],
    )


@patch("app.FirecrawlClient.from_environment")
def test_status_handler_rewrites_secret_upstream_next_url(from_environment, client):
    firecrawl = Mock()
    firecrawl.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "next": "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=10&limit=25",
        "data": [],
    }
    from_environment.return_value = firecrawl

    response = client.get("/api/crawl/crawl-123?trace=do-not-echo", headers=AUTH)
    payload = response.json()

    assert response.status_code == 200
    assert payload["next"] == (
        "https://scraper.example.com/api/crawl/crawl-123?skip=10&pageLimit=25"
    )
    assert "api.firecrawl.dev" not in payload["next"]
    assert "do-not-echo" not in response.text
    assert payload["dataIncluded"] is False
    assert "data" not in payload


@patch("app.download_attachments")
@patch("app.FirecrawlClient.from_environment")
def test_completed_status_returns_attachment_links(
    from_environment,
    download_attachments,
    client,
):
    firecrawl = Mock()
    firecrawl.get_crawl_status.return_value = {
        "success": True,
        "status": "completed",
        "completed": 1,
        "total": 1,
        "creditsUsed": 1,
        "data": [
            {
                "markdown": "A useful page with   normalized whitespace.",
                "links": [
                    "https://example.com/about",
                    "https://example.com/files/specification.pdf",
                    "https://example.com/files/specification.pdf",
                    "https://example.com/files/pricing.xlsx?version=2",
                ],
                "metadata": {
                    "title": "Example documentation",
                    "sourceURL": "https://example.com/docs",
                },
            }
        ],
    }
    from_environment.return_value = firecrawl
    download_attachments.return_value = {
        "status": "completed",
        "folder": "downloads/crawl-123",
        "total": 2,
        "downloaded": 2,
        "existing": 0,
        "failed": 0,
        "files": [],
    }

    response = client.get("/api/crawl/crawl-123", headers=AUTH)
    payload = response.json()

    assert response.status_code == 200
    assert payload["terminal"] is True
    assert payload["attachmentLinks"] == [
        "https://example.com/files/specification.pdf",
        "https://example.com/files/pricing.xlsx?version=2",
    ]
    assert payload["attachmentCount"] == 2
    assert payload["downloads"]["downloaded"] == 2
    assert "data" not in payload
    download_attachments.assert_called_once_with(
        "crawl-123",
        [
            "https://example.com/files/specification.pdf",
            "https://example.com/files/pricing.xlsx?version=2",
        ],
    )


@patch("app.FirecrawlClient.from_environment")
def test_raw_data_is_opt_in(from_environment, client):
    firecrawl = Mock()
    firecrawl.get_crawl_status.return_value = {
        "success": True,
        "status": "completed",
        "data": [{"markdown": "raw page", "metadata": {}}],
    }
    from_environment.return_value = firecrawl

    payload = client.get(
        "/api/crawl/crawl-123", headers=AUTH, params={"includeData": "true"}
    ).json()

    assert payload["dataIncluded"] is True
    assert payload["data"][0]["markdown"] == "raw page"


@patch("app.download_attachments")
@patch("app.FirecrawlClient.from_environment")
def test_completed_status_collects_attachments_from_all_result_pages(
    from_environment,
    download_attachments,
    client,
):
    firecrawl = Mock()
    firecrawl.get_crawl_status.return_value = {
        "success": True,
        "status": "completed",
        "data": [{"links": ["https://example.com/first.pdf"]}],
        "next": "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=1",
    }
    firecrawl.get_crawl_result_page.side_effect = [
        {
            "data": [
                {
                    "links": [
                        "https://example.com/first.pdf",
                        "https://example.com/second.docx",
                    ]
                }
            ],
            "next": None,
        }
    ]
    from_environment.return_value = firecrawl
    download_attachments.return_value = {
        "status": "completed",
        "folder": "downloads/crawl-123",
        "total": 2,
        "downloaded": 2,
        "existing": 0,
        "failed": 0,
        "files": [],
    }

    payload = client.get("/api/crawl/crawl-123", headers=AUTH).json()

    assert payload["attachmentLinks"] == [
        "https://example.com/first.pdf",
        "https://example.com/second.docx",
    ]
    assert payload["attachmentCount"] == 2
    assert payload["resultPagesFetched"] == 2
    assert payload["next"] is None
    firecrawl.get_crawl_result_page.assert_called_once_with(
        "crawl-123",
        "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=1",
    )


def test_start_handler_rejects_non_http_url(client):
    response = client.post("/api/crawl", headers=AUTH, json={"url": "file:///tmp/a"})

    assert response.status_code == 400
    assert response.json()["success"] is False


def test_start_handler_rejects_invalid_json(client):
    response = client.post(
        "/api/crawl",
        headers={**AUTH, "Content-Type": "application/json"},
        content=b"{not json",
    )

    assert response.status_code == 400
    assert response.json()["errorCode"] == "INVALID_REQUEST"


@patch("app.resolve_redirect_destination", return_value="https://example.com")
@patch("app.time")
@patch("app.FirecrawlClient.from_environment")
def test_start_handler_returns_timeout_when_crawl_does_not_finish(
    from_environment,
    time_module,
    resolve_redirect_destination,
    client,
    monkeypatch,
):
    monkeypatch.setenv("FIRECRAWL_POLL_TIMEOUT_SECONDS", "1")
    time_module.monotonic.side_effect = [0.0, 2.0]
    firecrawl = Mock()
    firecrawl.start_crawl.return_value = {"success": True, "id": "crawl-123"}
    firecrawl.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "data": [],
    }
    from_environment.return_value = firecrawl

    response = client.post(
        "/api/crawl", headers=AUTH, json={"url": "https://example.com", "limit": 1}
    )
    payload = response.json()

    assert response.status_code == 504
    assert payload["errorCode"] == "CRAWL_POLL_TIMEOUT"
    assert payload["jobId"] == "crawl-123"
    assert payload["terminal"] is False
    assert payload["retryable"] is True


@patch("app.time")
def test_internal_polling_retries_transient_rate_limit(time_module):
    time_module.monotonic.side_effect = [0.0, 0.0, 1.0]
    firecrawl = Mock()
    firecrawl.get_crawl_status.side_effect = [
        service.FirecrawlAPIError(429, {"error": "rate limited"}, "2"),
        {"success": True, "status": "completed", "data": []},
    ]

    result, attempts, elapsed = service.wait_for_crawl(firecrawl, "crawl-123")

    assert result["status"] == "completed"
    assert attempts == 2
    assert elapsed == 1.0
    time_module.sleep.assert_called_once_with(2)


def test_firecrawl_error_does_not_echo_arbitrary_upstream_payload():
    response = service.error_response(
        service.FirecrawlAPIError(
            400,
            {
                "error": "Bad crawl option",
                "submittedHeaders": {"Authorization": "target-secret"},
            },
        )
    )

    assert response.status_code == 400
    assert b"Bad crawl option" in response.body
    assert b"target-secret" not in response.body


@pytest.mark.parametrize(
    "headers, path",
    [
        ({}, "/api/crawl"),
        ({"x-api-key": "wrong"}, "/api/crawl"),
        ({"x-functions-key": API_KEY}, "/api/crawl"),
        ({}, f"/api/crawl?code={API_KEY}"),
    ],
)
@patch("app.FirecrawlClient.from_environment")
def test_rejects_missing_or_invalid_api_key(from_environment, client, headers, path):
    response = client.post(path, headers=headers, json={"url": "https://example.com"})

    assert response.status_code == 401
    assert response.json()["errorCode"] == "UNAUTHORIZED"
    from_environment.assert_not_called()


@patch("app.FirecrawlClient.from_environment")
def test_accepts_api_key_header(from_environment, client):
    from_environment.return_value.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "data": [],
    }

    response = client.get("/api/crawl/crawl-123", headers={"x-api-key": API_KEY})

    assert response.status_code == 200


def test_missing_api_keys_fails_closed(client, monkeypatch):
    monkeypatch.delenv("API_KEYS")

    response = client.get("/api/crawl/crawl-123", headers=AUTH)

    assert response.status_code == 500
    assert response.json()["errorCode"] == "CONFIGURATION_ERROR"


@patch("app.FirecrawlClient.from_environment")
def test_auth_can_be_disabled_explicitly(from_environment, client, monkeypatch):
    monkeypatch.delenv("API_KEYS")
    monkeypatch.setenv("API_AUTH_DISABLED", "true")
    from_environment.return_value.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "data": [],
    }

    assert client.get("/api/crawl/crawl-123").status_code == 200


def test_healthz_needs_no_api_key(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ambiguous_auth_disabled_value_fails_closed(client, monkeypatch):
    monkeypatch.setenv("API_AUTH_DISABLED", "on")

    response = client.get("/api/crawl/crawl-123")

    assert response.status_code == 500
    assert response.json()["errorCode"] == "CONFIGURATION_ERROR"


def test_json_response_tolerates_nan_from_upstream():
    response = service.json_response({"score": float("nan")}, 200)

    assert response.status_code == 200
    assert response.body == b'{"score": NaN}'


def test_unknown_log_level_falls_back_to_info(monkeypatch, caplog):
    monkeypatch.setenv("LOG_LEVEL", "verbose")
    basic_config = Mock()
    monkeypatch.setattr(service.logging, "basicConfig", basic_config)

    service._configure_logging()

    assert basic_config.call_args.kwargs["level"] == service.logging.INFO
    assert "Unknown LOG_LEVEL 'VERBOSE'" in caplog.text
