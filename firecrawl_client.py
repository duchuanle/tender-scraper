"""Small REST client and input validation for the Firecrawl Azure Function."""

from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Mapping
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from tender_artifacts import TENDER_JSON_FORMAT


DEFAULT_API_URL = "https://api.firecrawl.dev"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class ConfigurationError(ValueError):
    """Raised when required application settings are invalid or missing."""


class RequestValidationError(ValueError):
    """Raised when a caller submits an invalid crawl request."""


class FirecrawlAPIError(RuntimeError):
    """Raised when Firecrawl returns a non-success HTTP response."""

    def __init__(
        self,
        status_code: int,
        payload: Any,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(f"Firecrawl returned HTTP {status_code}")
        self.status_code = status_code
        self.payload = payload
        self.retry_after = retry_after


class FirecrawlConnectionError(RuntimeError):
    """Raised when the Firecrawl server cannot be reached."""


class RedirectResolutionError(RuntimeError):
    """Raised when a protected URL's redirect destination cannot be resolved."""


def _positive_int_setting(name: str, default: int, maximum: int) -> int:
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


def validate_target_url(value: Any) -> str:
    """Return a normalized HTTP(S) target URL or raise a validation error."""
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError("url is required and must be a string")

    url = value.strip()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise RequestValidationError("url is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise RequestValidationError("url must be an absolute http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise RequestValidationError("url must not contain embedded credentials")
    return url


def unwrap_safelink(url: str) -> str:
    """Extract the destination embedded in an Outlook Safe Links URL."""
    cleaned_url = unescape(url).strip()
    parsed = urlsplit(cleaned_url)
    hostname = (parsed.hostname or "").lower()
    if hostname == "safelinks.protection.outlook.com" or hostname.endswith(
        ".safelinks.protection.outlook.com"
    ):
        embedded_url = parse_qs(parsed.query).get("url", [cleaned_url])[0]
        return validate_target_url(embedded_url)
    return validate_target_url(cleaned_url)


def resolve_redirect_destination(
    url: str,
    *,
    session: requests.Session | None = None,
) -> str:
    """Resolve one protected/tracking redirect and return its destination URL."""
    input_url = validate_target_url(url)
    input_hostname = (urlsplit(input_url).hostname or "").lower()
    tracking_url = unwrap_safelink(input_url)
    tracking_hostname = (urlsplit(tracking_url).hostname or "").lower()

    is_safelink = (
        input_hostname == "safelinks.protection.outlook.com"
        or input_hostname.endswith(".safelinks.protection.outlook.com")
    )
    if not is_safelink and tracking_hostname != "connect.buy.nsw.gov.au":
        return tracking_url

    timeout = _positive_int_setting("URL_RESOLUTION_TIMEOUT_SECONDS", 20, 120)
    http = session or requests.Session()
    response: requests.Response | None = None
    try:
        response = http.get(
            tracking_url,
            allow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        location = response.headers.get("Location")
        if not location:
            return tracking_url
        return validate_target_url(urljoin(tracking_url, location))
    except requests.RequestException as exc:
        raise RedirectResolutionError(
            "Could not resolve the protected URL destination"
        ) from exc
    finally:
        if response is not None:
            response.close()


def validate_job_id(value: Any) -> str:
    if not isinstance(value, str) or not JOB_ID_PATTERN.fullmatch(value):
        raise RequestValidationError("invalid crawl job id")
    return value


def build_crawl_payload(body: Any) -> dict[str, Any]:
    """Validate a Firecrawl-compatible request and apply cost-safe defaults."""
    if not isinstance(body, Mapping):
        raise RequestValidationError("request body must be a JSON object")

    payload = dict(body)
    payload["url"] = validate_target_url(payload.get("url"))

    default_limit = _positive_int_setting("DEFAULT_CRAWL_LIMIT", 1, 10_000)
    max_limit = _positive_int_setting("MAX_CRAWL_LIMIT", 1_000, 10_000)
    if default_limit > max_limit:
        raise ConfigurationError(
            "DEFAULT_CRAWL_LIMIT must be less than or equal to MAX_CRAWL_LIMIT"
        )

    limit = payload.get("limit", default_limit)
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise RequestValidationError("limit must be an integer")
    if limit < 1 or limit > max_limit:
        raise RequestValidationError(f"limit must be between 1 and {max_limit}")
    payload["limit"] = limit

    payload.setdefault(
        "scrapeOptions",
        {"formats": [], "onlyMainContent": True},
    )
    if not isinstance(payload["scrapeOptions"], Mapping):
        raise RequestValidationError("scrapeOptions must be a JSON object")

    scrape_options = dict(payload["scrapeOptions"])
    formats = scrape_options.get("formats", [])
    if not isinstance(formats, list):
        raise RequestValidationError("scrapeOptions.formats must be an array")

    def format_type(value: Any) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping) and isinstance(value.get("type"), str):
            return value["type"]
        return None

    # This endpoint has a fixed output contract. Retain caller-requested extra
    # formats, but always use our tender schema for the JSON extraction.
    formats = [item for item in formats if format_type(item) != "json"]
    present_formats = {format_type(item) for item in formats}
    if "links" not in present_formats:
        formats.append("links")
    if "rawHtml" not in present_formats:
        formats.append("rawHtml")
    formats.append(copy.deepcopy(TENDER_JSON_FORMAT))
    scrape_options["formats"] = formats
    payload["scrapeOptions"] = scrape_options

    return payload


def parse_page_parameter(
    value: str | None,
    name: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RequestValidationError(f"{name} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise RequestValidationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return parsed


@dataclass
class FirecrawlClient:
    """Calls the Firecrawl v2 crawl endpoints."""

    api_root: str
    api_key: str | None
    timeout_seconds: int = 30
    session: requests.Session = field(default_factory=requests.Session)

    @classmethod
    def from_environment(cls) -> "FirecrawlClient":
        configured_url = os.getenv("FIRECRAWL_API_URL", DEFAULT_API_URL).strip()
        try:
            parsed = urlsplit(configured_url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise ConfigurationError("FIRECRAWL_API_URL is not a valid URL") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not hostname
        ):
            raise ConfigurationError(
                "FIRECRAWL_API_URL must be an absolute http:// or https:// URL"
            )
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ConfigurationError(
                "FIRECRAWL_API_URL must not contain credentials, a query, or a fragment"
            )

        api_root = configured_url.rstrip("/")
        if not re.search(r"/v[12]$", api_root, flags=re.IGNORECASE):
            api_root = f"{api_root}/v2"

        api_key = os.getenv("FIRECRAWL_API_KEY", "").strip() or None
        if hostname.lower() == "api.firecrawl.dev" and not api_key:
            raise ConfigurationError(
                "FIRECRAWL_API_KEY is required when using Firecrawl Cloud"
            )

        timeout = _positive_int_setting(
            "FIRECRAWL_REQUEST_TIMEOUT_SECONDS", 30, 120
        )
        return cls(api_root=api_root, api_key=api_key, timeout_seconds=timeout)

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "azure-firecrawl-function/1.0",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def start_crawl(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"{self.api_root}/crawl", json=dict(payload))

    def get_crawl_status(
        self,
        job_id: str,
        *,
        skip: int | None = None,
        page_limit: int | None = None,
    ) -> dict[str, Any]:
        job_id = validate_job_id(job_id)
        params: dict[str, int] = {}
        if skip is not None:
            params["skip"] = skip
        if page_limit is not None:
            params["limit"] = page_limit
        return self._request(
            "GET",
            f"{self.api_root}/crawl/{job_id}",
            params=params or None,
        )

    def get_crawl_result_page(self, job_id: str, page_url: str) -> dict[str, Any]:
        """Get a Firecrawl-generated next page for the specified crawl."""
        job_id = validate_job_id(job_id)
        expected = urlsplit(self.api_root)
        candidate = urlsplit(page_url)
        expected_path = f"{expected.path.rstrip('/')}/crawl/{job_id}"
        if (
            candidate.scheme.lower() != expected.scheme.lower()
            or candidate.netloc.lower() != expected.netloc.lower()
            or candidate.path != expected_path
        ):
            raise FirecrawlConnectionError(
                "Firecrawl returned an invalid crawl pagination URL"
            )
        return self._request("GET", page_url)

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                url,
                headers=self.headers,
                timeout=self.timeout_seconds,
                **kwargs,
            )
        except requests.Timeout as exc:
            raise FirecrawlConnectionError("Firecrawl request timed out") from exc
        except requests.RequestException as exc:
            raise FirecrawlConnectionError("Could not connect to Firecrawl") from exc

        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"error": "Firecrawl returned a non-JSON response"}

        if not response.ok:
            raise FirecrawlAPIError(
                response.status_code,
                payload,
                response.headers.get("Retry-After"),
            )
        if not isinstance(payload, dict):
            raise FirecrawlConnectionError("Firecrawl returned an invalid JSON response")
        return payload
