"""HTTP service (FastAPI) for synchronous Firecrawl tender workflows."""

from __future__ import annotations

import hmac
import json
import logging
import os
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .attachment_downloader import download_attachments
from .attachment_links import extract_attachment_links
from .firecrawl_client import (
    ConfigurationError,
    FirecrawlAPIError,
    FirecrawlClient,
    FirecrawlConnectionError,
    RedirectResolutionError,
    RequestValidationError,
    build_crawl_payload,
    parse_page_parameter,
    resolve_redirect_destination,
    validate_job_id,
)
from .onedrive_client import upload_job_outputs
from .settings import boolean_setting
from .tender_artifacts import (
    extract_tender_summary,
    save_crawl_artifacts,
    select_primary_document,
)


def _configure_logging() -> None:
    requested = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(requested)
    valid = isinstance(level, int)
    logging.basicConfig(
        level=level if valid else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not valid:
        logging.warning("Unknown LOG_LEVEL %r; using INFO", requested)


_configure_logging()

app = FastAPI(
    title="Tender Scraper",
    description=(
        "Resolves a tender link, crawls it with Firecrawl, extracts the tender "
        "summary, downloads attachments and optionally uploads them to OneDrive."
    ),
    version="1.0.0",
)

TERMINAL_CRAWL_STATUSES = {"completed", "failed", "cancelled"}
API_KEY_HEADER = "x-api-key"
CRAWL_REQUEST_SCHEMA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["url"],
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "Tender page, Outlook Safe Link or buy NSW tracking URL.",
                        },
                        "limit": {"type": "integer", "minimum": 1, "default": 1},
                    },
                }
            }
        },
    }
}


class CompatJSONResponse(JSONResponse):
    """JSON response that tolerates NaN/Infinity from upstream data.

    Starlette's JSONResponse refuses them, which would turn an already
    processed crawl into a 500.
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


class AuthenticationError(RuntimeError):
    """Raised when a request does not carry a valid API key."""


class CrawlPollingTimeout(RuntimeError):
    """Raised when Firecrawl has not finished within this invocation's limit."""

    def __init__(self, job_id: str, last_status: str, timeout_seconds: int) -> None:
        super().__init__(f"Crawl did not finish within {timeout_seconds} seconds")
        self.job_id = job_id
        self.last_status = last_status
        self.timeout_seconds = timeout_seconds


def positive_int_setting(name: str, default: int, maximum: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 1 or value > maximum:
        raise ConfigurationError(f"{name} must be between 1 and {maximum}")
    return value


def wait_for_crawl(
    client: FirecrawlClient,
    job_id: str,
) -> tuple[dict[str, Any], int, float]:
    """Poll Firecrawl until completion or the configured invocation deadline."""
    poll_interval = positive_int_setting("FIRECRAWL_POLL_INTERVAL_SECONDS", 5, 60)
    poll_timeout = positive_int_setting("FIRECRAWL_POLL_TIMEOUT_SECONDS", 240, 480)
    started_at = time.monotonic()
    attempts = 0
    last_status = "unknown"

    while True:
        attempts += 1
        retry_delay = poll_interval
        try:
            result = client.get_crawl_status(job_id)
            last_status = str(result.get("status") or "unknown").lower()
            if last_status in TERMINAL_CRAWL_STATUSES:
                return result, attempts, time.monotonic() - started_at
        except FirecrawlAPIError as exc:
            if exc.status_code != 429 and exc.status_code < 500:
                raise
            if exc.retry_after and exc.retry_after.isdigit():
                retry_delay = min(max(1, int(exc.retry_after)), 60)
            logging.warning(
                "Transient Firecrawl HTTP %s while polling job %s; retrying",
                exc.status_code,
                job_id,
            )
        except FirecrawlConnectionError as exc:
            logging.warning(
                "Transient Firecrawl connection error while polling job %s: %s",
                job_id,
                exc,
            )

        elapsed = time.monotonic() - started_at
        if elapsed >= poll_timeout:
            raise CrawlPollingTimeout(job_id, last_status, poll_timeout)

        time.sleep(min(retry_delay, max(0.0, poll_timeout - elapsed)))


def json_response(
    payload: Any,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return CompatJSONResponse(content=payload, status_code=status_code, headers=headers)


def clean_request_url(request_url: str) -> str:
    parsed = urlsplit(request_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def authenticate(headers: Mapping[str, str]) -> None:
    """Require one of API_KEYS unless API_AUTH_DISABLED is explicitly set."""
    try:
        auth_disabled = boolean_setting("API_AUTH_DISABLED")
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc
    if auth_disabled:
        return
    keys = [key.strip() for key in os.getenv("API_KEYS", "").split(",") if key.strip()]
    if not keys:
        raise ConfigurationError("API_KEYS must be set unless API_AUTH_DISABLED=true")
    supplied = headers.get(API_KEY_HEADER)
    if supplied and any(hmac.compare_digest(supplied.encode(), key.encode()) for key in keys):
        return
    raise AuthenticationError("Missing or invalid API key")


def boolean_query_parameter(value: str | None, name: str) -> bool:
    if value is None or value == "":
        return False
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise RequestValidationError(f"{name} must be true or false")


def error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, AuthenticationError):
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "UNAUTHORIZED",
                "error": str(exc),
                "retryable": False,
            },
            401,
        )
    if isinstance(exc, RequestValidationError):
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "INVALID_REQUEST",
                "error": str(exc),
                "retryable": False,
            },
            400,
        )
    if isinstance(exc, ConfigurationError):
        logging.error("Service configuration error: %s", exc)
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "CONFIGURATION_ERROR",
                "error": "The crawler service is not configured correctly",
                "retryable": False,
            },
            500,
        )
    if isinstance(exc, FirecrawlAPIError):
        status = exc.status_code if 400 <= exc.status_code < 500 else 502
        headers = {"Retry-After": exc.retry_after} if exc.retry_after else None
        firecrawl_message = "Firecrawl request failed"
        if isinstance(exc.payload, dict) and isinstance(exc.payload.get("error"), str):
            # Return the useful upstream message, not the full payload, which
            # could echo crawl options such as target-site request headers.
            firecrawl_message = exc.payload["error"][:1_000]
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "FIRECRAWL_ERROR",
                "error": "Firecrawl rejected the request",
                "firecrawlStatus": exc.status_code,
                "firecrawlMessage": firecrawl_message,
                "retryable": exc.status_code == 429 or exc.status_code >= 500,
            },
            status,
            headers,
        )
    if isinstance(exc, FirecrawlConnectionError):
        logging.warning("Firecrawl connection error: %s", exc)
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "FIRECRAWL_UNAVAILABLE",
                "error": str(exc),
                "retryable": True,
            },
            502,
        )
    if isinstance(exc, RedirectResolutionError):
        logging.warning("Protected URL resolution error: %s", exc)
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": "error",
                "errorCode": "URL_RESOLUTION_ERROR",
                "error": str(exc),
                "retryable": True,
            },
            502,
        )
    if isinstance(exc, CrawlPollingTimeout):
        logging.warning(
            "Firecrawl crawl job %s timed out while status=%s",
            exc.job_id,
            exc.last_status,
        )
        return json_response(
            {
                "success": False,
                "operation": "crawl",
                "status": exc.last_status,
                "terminal": False,
                "jobId": exc.job_id,
                "errorCode": "CRAWL_POLL_TIMEOUT",
                "error": str(exc),
                "retryable": True,
            },
            504,
        )

    logging.exception("Unexpected error while processing a crawl request")
    return json_response(
        {
            "success": False,
            "operation": "crawl",
            "status": "error",
            "errorCode": "INTERNAL_ERROR",
            "error": "Internal server error",
            "retryable": False,
        },
        500,
    )


def build_crawl_result(
    request_url: str,
    client: FirecrawlClient,
    job_id: str,
    result: dict[str, Any],
    *,
    include_data: bool = False,
    target_url: str | None = None,
) -> dict[str, Any]:
    """Build the stable response and save outputs after completion."""
    response = dict(result)
    response["id"] = job_id
    response["jobId"] = job_id
    response["operation"] = "crawl"

    status = str(result.get("status") or "unknown").lower()
    terminal = status in TERMINAL_CRAWL_STATUSES
    result_documents = result.get("data")
    attachment_links = extract_attachment_links(result_documents)
    seen_attachment_links = set(attachment_links)
    result_pages_fetched = 1

    firecrawl_next = result.get("next")
    seen_result_pages: set[str] = set()
    all_documents = list(result_documents) if isinstance(result_documents, list) else []
    if terminal:
        while isinstance(firecrawl_next, str) and firecrawl_next:
            if firecrawl_next in seen_result_pages:
                raise FirecrawlConnectionError(
                    "Firecrawl returned a repeated crawl pagination URL"
                )
            seen_result_pages.add(firecrawl_next)
            result_page = client.get_crawl_result_page(job_id, firecrawl_next)
            result_pages_fetched += 1
            page_documents = result_page.get("data")
            for link in extract_attachment_links(page_documents):
                if link not in seen_attachment_links:
                    seen_attachment_links.add(link)
                    attachment_links.append(link)
            if isinstance(page_documents, list):
                all_documents.extend(page_documents)
            firecrawl_next = result_page.get("next")

    primary_document = select_primary_document(all_documents, target_url)
    summary = extract_tender_summary(primary_document, target_url)
    primary_html = None
    if primary_document:
        primary_html = primary_document.get("rawHtml") or primary_document.get("html")

    response["success"] = bool(
        result.get("success", status not in {"failed", "cancelled"})
    )
    response["status"] = status
    response["terminal"] = terminal
    response["progress"] = {
        "completed": result.get("completed", 0),
        "total": result.get("total", 0),
        "creditsUsed": result.get("creditsUsed"),
    }
    response["attachmentCount"] = len(attachment_links)
    response["attachmentLinks"] = attachment_links
    response["resultPagesFetched"] = result_pages_fetched
    response["summary"] = summary
    if status == "completed":
        response["downloads"] = download_attachments(job_id, attachment_links)
        response["artifacts"] = save_crawl_artifacts(
            job_id,
            summary,
            primary_html,
        )
        if response["downloads"]["folder"] is None:
            response["downloads"]["folder"] = response["artifacts"]["folder"]
        response["oneDrive"] = upload_job_outputs(
            job_id,
            response["artifacts"],
            response["downloads"],
            summary=summary,
        )
    else:
        response["downloads"] = {
            "status": "pending" if not terminal else "not_started",
            "folder": None,
            "total": 0,
            "downloaded": 0,
            "existing": 0,
            "failed": 0,
            "files": [],
        }
        response["artifacts"] = {
            "status": "pending" if not terminal else "not_started",
            "folder": None,
            "summary": {"status": "not_started"},
            "html": {"status": "not_started"},
        }
        response["oneDrive"] = {"status": "not_started", "files": []}

    response["dataIncluded"] = include_data
    if include_data:
        response["data"] = all_documents
    else:
        response.pop("data", None)

    messages = {
        "completed": "Crawl completed and outputs were processed.",
        "failed": "Crawl failed. Review the returned error and service logs.",
        "cancelled": "Crawl was cancelled.",
    }
    response["message"] = messages.get(status, "Crawl is still in progress.")
    response["next"] = None if terminal else response.get("next")

    # Never expose Firecrawl's authenticated pagination endpoint to the caller.
    if not terminal and isinstance(firecrawl_next, str) and firecrawl_next:
        next_query = parse_qs(urlsplit(firecrawl_next).query)
        local_query: dict[str, str] = {}
        if next_query.get("skip"):
            local_query["skip"] = next_query["skip"][0]
        if next_query.get("limit"):
            local_query["pageLimit"] = next_query["limit"][0]
        if include_data:
            local_query["includeData"] = "true"
        next_url = clean_request_url(request_url)
        if local_query:
            next_url = f"{next_url}?{urlencode(local_query)}"
        response["next"] = next_url

    return response


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """Unauthenticated liveness probe for Docker and load balancers.

    async so it never waits for a thread-pool slot held by a long crawl.
    """
    return {"status": "ok"}


@app.post("/api/crawl", openapi_extra=CRAWL_REQUEST_SCHEMA)
async def start_crawl(request: Request) -> JSONResponse:
    """Start Firecrawl, wait for it to finish, then download attachments."""
    try:
        authenticate(request.headers)
        raw_body = await request.body()
    except Exception as exc:  # Converted to a stable HTTP contract in one place.
        return error_response(exc)
    # The workflow blocks on HTTP calls and polling, so keep it off the event loop.
    return await run_in_threadpool(run_crawl, str(request.url), raw_body)


def run_crawl(request_url: str, raw_body: bytes) -> JSONResponse:
    try:
        try:
            body = json.loads(raw_body)
        except ValueError as exc:
            raise RequestValidationError("request body must contain valid JSON") from exc

        payload = build_crawl_payload(body)
        input_url = payload["url"]
        resolved_url = resolve_redirect_destination(input_url)
        payload["url"] = resolved_url
        client = FirecrawlClient.from_environment()
        start_result = client.start_crawl(payload)
        job_id = start_result.get("id")
        if not isinstance(job_id, str) or not job_id:
            raise FirecrawlConnectionError("Firecrawl did not return a crawl job id")

        job_id = validate_job_id(job_id)
        logging.info("Started Firecrawl crawl job %s; polling internally", job_id)
        result, polling_attempts, polling_elapsed = wait_for_crawl(client, job_id)
        response = build_crawl_result(
            request_url,
            client,
            job_id,
            result,
            target_url=resolved_url,
        )
        response["statusUrl"] = f"{clean_request_url(request_url)}/{job_id}"
        response["inputUrl"] = input_url
        response["resolvedUrl"] = resolved_url
        response["polling"] = {
            "attempts": polling_attempts,
            "elapsedSeconds": round(polling_elapsed, 3),
        }
        logging.info(
            "Firecrawl crawl job %s reached terminal status=%s after %s polls",
            job_id,
            response["status"],
            polling_attempts,
        )
        return json_response(response, 200)
    except Exception as exc:  # Converted to a stable HTTP contract in one place.
        return error_response(exc)


@app.get("/api/crawl/{job_id}")
def get_crawl_status(job_id: str, request: Request) -> JSONResponse:
    """Poll crawl progress and return discovered attachment links."""
    try:
        authenticate(request.headers)
        job_id = validate_job_id(job_id)
        params = request.query_params
        skip = parse_page_parameter(params.get("skip"), "skip", 0, 10_000_000)
        page_limit = parse_page_parameter(
            params.get("pageLimit"), "pageLimit", 1, 10_000
        )
        include_data = boolean_query_parameter(
            params.get("includeData"), "includeData"
        )

        client = FirecrawlClient.from_environment()
        result = client.get_crawl_status(
            job_id,
            skip=skip,
            page_limit=page_limit,
        )
        response = build_crawl_result(
            str(request.url),
            client,
            job_id,
            result,
            include_data=include_data,
        )

        logging.info(
            "Firecrawl crawl job %s status=%s completed=%s total=%s",
            job_id,
            response["status"],
            response["progress"]["completed"],
            response["progress"]["total"],
        )

        return json_response(response, 200)
    except Exception as exc:  # Converted to a stable HTTP contract in one place.
        return error_response(exc)
