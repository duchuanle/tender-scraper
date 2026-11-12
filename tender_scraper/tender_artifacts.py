"""Extract and save structured tender data and source-page HTML."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from .attachment_downloader import get_job_output_folder


TENDER_FIELDS = (
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
)

TENDER_JSON_FORMAT: dict[str, Any] = {
    "type": "json",
    "prompt": (
        "Extract the tender opportunity details from this page. Copy values "
        "faithfully and do not invent missing information. Return an empty "
        "string when a value is unavailable. For contact, combine the name, "
        "email address, and phone number using single spaces. Return url as "
        "the plain canonical opportunity URL, never as a Markdown link."
    ),
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "Full opportunity title"},
            "agency": {
                "type": "string",
                "description": "Managing or publishing agency",
            },
            "tenderID": {
                "type": "string",
                "description": "Opportunity or tender ID",
            },
            "category": {
                "type": "string",
                "description": "All categories joined with comma and space",
            },
            "closeDate": {
                "type": "string",
                "description": "Closing date and time as displayed on the page",
            },
            "locations": {
                "type": "string",
                "description": "Tender locations or regions",
            },
            "type": {
                "type": "string",
                "description": "Opportunity type and status",
            },
            "description": {
                "type": "string",
                "description": "Tender description copied from the page",
            },
            "contact": {
                "type": "string",
                "description": "Contact name, email, and phone number",
            },
            "url": {
                "type": "string",
                "description": "Plain canonical tender opportunity URL",
            },
        },
        "required": list(TENDER_FIELDS),
    },
}


def _comparable_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, "")
    )


def select_primary_document(
    documents: Iterable[Any],
    target_url: str | None,
) -> Mapping[str, Any] | None:
    """Select the starting URL's document, falling back to the first result."""
    candidates = [item for item in documents if isinstance(item, Mapping)]
    comparable_target = _comparable_url(target_url)
    if comparable_target:
        for document in candidates:
            metadata = document.get("metadata")
            if not isinstance(metadata, Mapping):
                continue
            for key in ("sourceURL", "url"):
                if _comparable_url(metadata.get(key)) == comparable_target:
                    return document
    return candidates[0] if candidates else None


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return ", ".join(_string_value(item) for item in value if item is not None)
    return str(value).strip()


def extract_tender_summary(
    document: Mapping[str, Any] | None,
    target_url: str | None,
) -> dict[str, str] | None:
    """Normalize Firecrawl JSON extraction into the stable tender contract."""
    if not document or not isinstance(document.get("json"), Mapping):
        return None
    extracted = document["json"]
    summary = {field: _string_value(extracted.get(field)) for field in TENDER_FIELDS}
    if target_url:
        summary["url"] = target_url
    return summary


def _saved_file(path: Path, content: bytes) -> dict[str, Any]:
    status = "existing"
    if not path.exists():
        temporary_path = path.with_suffix(path.suffix + ".part")
        try:
            temporary_path.write_bytes(content)
            temporary_path.replace(path)
            status = "saved"
        finally:
            temporary_path.unlink(missing_ok=True)
    saved_content = path.read_bytes()
    return {
        "status": status,
        "fileName": path.name,
        "localPath": str(path),
        "sizeBytes": len(saved_content),
        "sha256": hashlib.sha256(saved_content).hexdigest(),
    }


def save_crawl_artifacts(
    job_id: str,
    summary: Mapping[str, str] | None,
    html: Any,
    *,
    download_root: Path | None = None,
) -> dict[str, Any]:
    """Save summary.json and website.html beside downloaded attachments."""
    output_folder = get_job_output_folder(
        job_id,
        download_root=download_root,
        create=True,
    )

    if summary is None:
        summary_file = {"status": "missing", "error": "No structured JSON returned"}
    else:
        summary_content = json.dumps(
            dict(summary), ensure_ascii=False, indent=2
        ).encode("utf-8")
        summary_file = _saved_file(output_folder / "summary.json", summary_content)

    if not isinstance(html, str) or not html.strip():
        html_file = {"status": "missing", "error": "No HTML returned"}
    else:
        html_file = _saved_file(output_folder / "website.html", html.encode("utf-8"))

    missing = sum(
        item["status"] == "missing" for item in (summary_file, html_file)
    )
    return {
        "status": "completed_with_errors" if missing else "completed",
        "folder": str(output_folder),
        "summary": summary_file,
        "html": html_file,
    }
