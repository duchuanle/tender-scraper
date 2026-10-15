import json
from unittest.mock import Mock, patch

import azure.functions as func
import pytest

import function_app


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
    monkeypatch.setattr(function_app, "save_crawl_artifacts", saver)
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
    monkeypatch.setattr(function_app, "upload_job_outputs", uploader)
    return uploader


def make_request(
    method: str,
    url: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
    route_params: dict | None = None,
) -> func.HttpRequest:
    return func.HttpRequest(
        method=method,
        url=url,
        headers={"Content-Type": "application/json"},
        params=params or {},
        route_params=route_params or {},
        body=json.dumps(body).encode() if body is not None else b"",
    )


def response_json(response: func.HttpResponse) -> dict:
    return json.loads(response.get_body())


@patch("function_app.resolve_redirect_destination")
@patch("function_app.download_attachments")
@patch("function_app.time.sleep")
@patch("function_app.FirecrawlClient.from_environment")
def test_start_handler_polls_until_complete_and_downloads(
    from_environment,
    sleep,
    download_attachments,
    resolve_redirect_destination,
    mock_save_artifacts,
    mock_onedrive_upload,
    monkeypatch,
):
    monkeypatch.delenv("DEFAULT_CRAWL_LIMIT", raising=False)
    monkeypatch.delenv("MAX_CRAWL_LIMIT", raising=False)
    client = Mock()
    client.start_crawl.return_value = {
        "success": True,
        "id": "crawl-123",
        "url": "https://api.firecrawl.dev/v2/crawl/crawl-123",
    }
    client.get_crawl_status.side_effect = [
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
    from_environment.return_value = client
    resolve_redirect_destination.return_value = "https://example.com/final"
    request = make_request(
        "POST",
        "https://my-function.azurewebsites.net/api/crawl?code=do-not-echo",
        body={"url": "https://example.com", "limit": 2},
    )

    response = function_app.start_crawl(request)

    assert response.status_code == 200
    assert response_json(response)["statusUrl"] == (
        "https://my-function.azurewebsites.net/api/crawl/crawl-123"
    )
    assert "do-not-echo" not in response.get_body().decode()
    assert response_json(response)["status"] == "completed"
    assert response_json(response)["terminal"] is True
    assert response_json(response)["attachmentLinks"] == [
        "https://example.com/files/specification.pdf"
    ]
    assert response_json(response)["downloads"]["downloaded"] == 1
    assert response_json(response)["polling"]["attempts"] == 2
    assert response_json(response)["resolvedUrl"] == "https://example.com/final"
    assert response_json(response)["summary"]["name"] == "Tender title"
    assert response_json(response)["summary"]["url"] == "https://example.com/final"
    submitted_payload = client.start_crawl.call_args.args[0]
    assert submitted_payload["url"] == "https://example.com/final"
    assert submitted_payload["limit"] == 2
    formats = submitted_payload["scrapeOptions"]["formats"]
    assert formats[:2] == ["links", "rawHtml"]
    assert formats[2]["type"] == "json"
    assert client.get_crawl_status.call_count == 2
    sleep.assert_called_once_with(5)
    download_attachments.assert_called_once_with(
        "crawl-123", ["https://example.com/files/specification.pdf"]
    )
    mock_save_artifacts.assert_called_once_with(
        "crawl-123",
        response_json(response)["summary"],
        "<html><h1>Tender title</h1></html>",
    )
    mock_onedrive_upload.assert_called_once_with(
        "crawl-123",
        response_json(response)["artifacts"],
        response_json(response)["downloads"],
        summary=response_json(response)["summary"],
    )


@patch("function_app.FirecrawlClient.from_environment")
def test_status_handler_rewrites_secret_upstream_next_url(from_environment):
    client = Mock()
    client.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "next": "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=10&limit=25",
        "data": [],
    }
    from_environment.return_value = client
    request = make_request(
        "GET",
        "https://my-function.azurewebsites.net/api/crawl/crawl-123?code=secret",
        route_params={"job_id": "crawl-123"},
    )

    response = function_app.get_crawl_status(request)

    assert response.status_code == 200
    assert response_json(response)["next"] == (
        "https://my-function.azurewebsites.net/api/crawl/crawl-123?skip=10&pageLimit=25"
    )
    assert "api.firecrawl.dev" not in response_json(response)["next"]
    assert response_json(response)["dataIncluded"] is False
    assert "data" not in response_json(response)


@patch("function_app.download_attachments")
@patch("function_app.FirecrawlClient.from_environment")
def test_completed_status_returns_attachment_links(
    from_environment,
    download_attachments,
):
    client = Mock()
    client.get_crawl_status.return_value = {
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
    from_environment.return_value = client
    download_attachments.return_value = {
        "status": "completed",
        "folder": "downloads/crawl-123",
        "total": 2,
        "downloaded": 2,
        "existing": 0,
        "failed": 0,
        "files": [],
    }
    request = make_request(
        "GET",
        "https://my-function.azurewebsites.net/api/crawl/crawl-123",
        route_params={"job_id": "crawl-123"},
    )

    response = function_app.get_crawl_status(request)
    payload = response_json(response)

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


@patch("function_app.FirecrawlClient.from_environment")
def test_raw_data_is_opt_in(from_environment):
    client = Mock()
    client.get_crawl_status.return_value = {
        "success": True,
        "status": "completed",
        "data": [{"markdown": "raw page", "metadata": {}}],
    }
    from_environment.return_value = client
    request = make_request(
        "GET",
        "https://my-function.azurewebsites.net/api/crawl/crawl-123",
        params={"includeData": "true"},
        route_params={"job_id": "crawl-123"},
    )

    payload = response_json(function_app.get_crawl_status(request))

    assert payload["dataIncluded"] is True
    assert payload["data"][0]["markdown"] == "raw page"


@patch("function_app.download_attachments")
@patch("function_app.FirecrawlClient.from_environment")
def test_completed_status_collects_attachments_from_all_result_pages(
    from_environment,
    download_attachments,
):
    client = Mock()
    client.get_crawl_status.return_value = {
        "success": True,
        "status": "completed",
        "data": [{"links": ["https://example.com/first.pdf"]}],
        "next": "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=1",
    }
    client.get_crawl_result_page.side_effect = [
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
    from_environment.return_value = client
    download_attachments.return_value = {
        "status": "completed",
        "folder": "downloads/crawl-123",
        "total": 2,
        "downloaded": 2,
        "existing": 0,
        "failed": 0,
        "files": [],
    }
    request = make_request(
        "GET",
        "https://my-function.azurewebsites.net/api/crawl/crawl-123",
        route_params={"job_id": "crawl-123"},
    )

    payload = response_json(function_app.get_crawl_status(request))

    assert payload["attachmentLinks"] == [
        "https://example.com/first.pdf",
        "https://example.com/second.docx",
    ]
    assert payload["attachmentCount"] == 2
    assert payload["resultPagesFetched"] == 2
    assert payload["next"] is None
    client.get_crawl_result_page.assert_called_once_with(
        "crawl-123",
        "https://api.firecrawl.dev/v2/crawl/crawl-123?skip=1",
    )


def test_start_handler_rejects_non_http_url():
    request = make_request("POST", "http://localhost/api/crawl", body={"url": "file:///tmp/a"})

    response = function_app.start_crawl(request)

    assert response.status_code == 400
    assert response_json(response)["success"] is False


@patch("function_app.resolve_redirect_destination", return_value="https://example.com")
@patch("function_app.time.monotonic", side_effect=[0.0, 2.0])
@patch("function_app.FirecrawlClient.from_environment")
def test_start_handler_returns_timeout_when_crawl_does_not_finish(
    from_environment,
    monotonic,
    resolve_redirect_destination,
    monkeypatch,
):
    monkeypatch.setenv("FIRECRAWL_POLL_TIMEOUT_SECONDS", "1")
    client = Mock()
    client.start_crawl.return_value = {"success": True, "id": "crawl-123"}
    client.get_crawl_status.return_value = {
        "success": True,
        "status": "scraping",
        "data": [],
    }
    from_environment.return_value = client
    request = make_request(
        "POST",
        "https://my-function.azurewebsites.net/api/crawl",
        body={"url": "https://example.com", "limit": 1},
    )

    response = function_app.start_crawl(request)
    payload = response_json(response)

    assert response.status_code == 504
    assert payload["errorCode"] == "CRAWL_POLL_TIMEOUT"
    assert payload["jobId"] == "crawl-123"
    assert payload["terminal"] is False
    assert payload["retryable"] is True


@patch("function_app.time.sleep")
@patch("function_app.time.monotonic", side_effect=[0.0, 0.0, 1.0])
def test_internal_polling_retries_transient_rate_limit(monotonic, sleep):
    client = Mock()
    client.get_crawl_status.side_effect = [
        function_app.FirecrawlAPIError(429, {"error": "rate limited"}, "2"),
        {"success": True, "status": "completed", "data": []},
    ]

    result, attempts, elapsed = function_app.wait_for_crawl(client, "crawl-123")

    assert result["status"] == "completed"
    assert attempts == 2
    assert elapsed == 1.0
    sleep.assert_called_once_with(2)


def test_firecrawl_error_does_not_echo_arbitrary_upstream_payload():
    response = function_app.error_response(
        function_app.FirecrawlAPIError(
            400,
            {
                "error": "Bad crawl option",
                "submittedHeaders": {"Authorization": "target-secret"},
            },
        )
    )

    payload = response_json(response)
    assert response.status_code == 400
    assert payload["firecrawlMessage"] == "Bad crawl option"
    assert "target-secret" not in response.get_body().decode()
