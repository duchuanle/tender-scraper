"""Download discovered attachment URLs to a job-specific local folder."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit

import requests


DEFAULT_DOWNLOAD_ROOT = Path(__file__).resolve().parent / "downloads"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
CONTENT_TYPE_EXTENSIONS = {
    "application/json": ".json",
    "application/msword": ".doc",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/zip": ".zip",
    "text/csv": ".csv",
    "text/plain": ".txt",
}


def _download_root() -> Path:
    configured = os.getenv("DOWNLOAD_DIR", "").strip()
    if not configured:
        return DEFAULT_DOWNLOAD_ROOT
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path.resolve()


def get_job_output_folder(
    job_id: str,
    *,
    download_root: Path | None = None,
    create: bool = False,
) -> Path:
    """Return a traversal-safe local output folder for one crawl job."""
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise ValueError("invalid crawl job id")
    root = (download_root or _download_root()).resolve()
    output_folder = (root / job_id).resolve()
    if output_folder.parent != root:
        raise ValueError("invalid crawl job output folder")
    if create:
        output_folder.mkdir(parents=True, exist_ok=True)
    return output_folder


def _timeout_seconds() -> int:
    raw_value = os.getenv("ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS", "60").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS must be an integer") from exc
    if value < 1 or value > 600:
        raise ValueError(
            "ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS must be between 1 and 600"
        )
    return value


def _url_digest(source_url: str) -> str:
    return hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:10]


def _candidate_filename(response: requests.Response, source_url: str) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    utf8_match = re.search(
        r"filename\*\s*=\s*UTF-8''([^;]+)",
        disposition,
        flags=re.IGNORECASE,
    )
    regular_match = re.search(
        r'filename\s*=\s*"?([^";]+)',
        disposition,
        flags=re.IGNORECASE,
    )
    if utf8_match:
        candidate = unquote(utf8_match.group(1).strip())
    elif regular_match:
        candidate = regular_match.group(1).strip()
    else:
        response_path = urlsplit(response.url or source_url).path
        candidate = unquote(Path(response_path).name)

    candidate = Path(candidate.replace("\\", "/")).name
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", candidate).strip(" .")
    if not candidate:
        candidate = "attachment"

    suffix = Path(candidate).suffix
    if not suffix:
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        suffix = CONTENT_TYPE_EXTENSIONS.get(content_type.lower(), ".bin")
    stem = Path(candidate).stem[:140] or "attachment"
    return f"{stem}{suffix.lower()}"


def _disambiguate(file_name: str, source_url: str) -> str:
    path = Path(file_name)
    return f"{path.stem}_{_url_digest(source_url)}{path.suffix}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_attachments(
    job_id: str,
    urls: Iterable[str],
    *,
    session: requests.Session | None = None,
    download_root: Path | None = None,
) -> dict[str, Any]:
    """GET each URL and save its response body under its original filename.

    Filenames are stable across crawls, so the storage upload replaces the
    previous copy. A short source-URL hash is added only when two different
    URLs in the same call resolve to the same filename.
    """
    source_urls = list(urls)
    output_folder = get_job_output_folder(
        job_id,
        download_root=download_root,
        create=bool(source_urls),
    )

    http = session or requests.Session()
    owns_session = session is None
    timeout = _timeout_seconds()
    files: list[dict[str, Any]] = []
    claimed_names: dict[str, str] = {}

    try:
        for source_url in source_urls:
            response: requests.Response | None = None
            temporary_path: Path | None = None
            try:
                response = http.get(
                    source_url,
                    allow_redirects=True,
                    stream=True,
                    timeout=timeout,
                    headers={
                        "Accept": "*/*",
                        "User-Agent": "Mozilla/5.0",
                    },
                )
                response.raise_for_status()
                file_name = _candidate_filename(response, source_url)
                if claimed_names.get(file_name.lower(), source_url) != source_url:
                    file_name = _disambiguate(file_name, source_url)
                claimed_names.setdefault(file_name.lower(), source_url)
                output_path = output_folder / file_name

                if output_path.exists():
                    files.append(
                        {
                            "status": "existing",
                            "sourceUrl": source_url,
                            "finalUrl": response.url,
                            "fileName": file_name,
                            "localPath": str(output_path),
                            "contentType": response.headers.get("Content-Type"),
                            "sizeBytes": output_path.stat().st_size,
                            "sha256": _file_sha256(output_path),
                        }
                    )
                    continue

                temporary_path = output_path.with_suffix(output_path.suffix + ".part")
                digest = hashlib.sha256()
                size_bytes = 0
                with temporary_path.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        if not chunk:
                            continue
                        file_handle.write(chunk)
                        digest.update(chunk)
                        size_bytes += len(chunk)
                temporary_path.replace(output_path)
                files.append(
                    {
                        "status": "downloaded",
                        "sourceUrl": source_url,
                        "finalUrl": response.url,
                        "fileName": file_name,
                        "localPath": str(output_path),
                        "contentType": response.headers.get("Content-Type"),
                        "sizeBytes": size_bytes,
                        "sha256": digest.hexdigest(),
                    }
                )
            except (OSError, ValueError, requests.RequestException) as exc:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
                files.append(
                    {
                        "status": "failed",
                        "sourceUrl": source_url,
                        "error": str(exc),
                    }
                )
            finally:
                if response is not None:
                    response.close()
    finally:
        if owns_session:
            http.close()

    downloaded = sum(item["status"] == "downloaded" for item in files)
    existing = sum(item["status"] == "existing" for item in files)
    failed = sum(item["status"] == "failed" for item in files)
    return {
        "status": "completed_with_errors" if failed else "completed",
        "folder": str(output_folder) if source_urls else None,
        "total": len(source_urls),
        "downloaded": downloaded,
        "existing": existing,
        "failed": failed,
        "files": files,
    }
