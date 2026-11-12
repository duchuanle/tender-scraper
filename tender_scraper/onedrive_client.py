"""Minimal delegated Microsoft Graph uploader for a personal OneDrive POC."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import requests

from .settings import boolean_setting


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
ONEDRIVE_SCOPE = "offline_access https://graph.microsoft.com/Files.ReadWrite"
MAX_SIMPLE_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_TENDER_FOLDER_NAME_LENGTH = 200


class OneDriveError(RuntimeError):
    """Raised for safe-to-report OneDrive configuration or API failures."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _enabled() -> bool:
    try:
        return boolean_setting("ONEDRIVE_ENABLED")
    except ValueError as exc:
        raise OneDriveError(str(exc)) from exc


def _timeout_seconds() -> int:
    raw_value = os.getenv("ONEDRIVE_REQUEST_TIMEOUT_SECONDS", "120").strip()
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise OneDriveError("ONEDRIVE_REQUEST_TIMEOUT_SECONDS must be an integer") from exc
    if value < 1 or value > 600:
        raise OneDriveError(
            "ONEDRIVE_REQUEST_TIMEOUT_SECONDS must be between 1 and 600"
        )
    return value


def _folder_segments(value: str) -> list[str]:
    segments = [segment.strip() for segment in value.replace("\\", "/").split("/")]
    segments = [segment for segment in segments if segment]
    if not segments:
        raise OneDriveError("ONEDRIVE_FOLDER must contain a folder name")
    for segment in segments:
        if segment in {".", ".."} or re.search(r'["*:<>?/\\|]', segment):
            raise OneDriveError("ONEDRIVE_FOLDER contains an invalid OneDrive name")
        if segment.endswith((".", " ")):
            raise OneDriveError("ONEDRIVE_FOLDER contains an invalid OneDrive name")
    return segments


def _clean_folder_name(value: str) -> str:
    cleaned = re.sub(r'["*:<>?/\\|\x00-\x1f]', "-", value)
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:MAX_TENDER_FOLDER_NAME_LENGTH].rstrip(" .")
    return cleaned


def tender_folder_name(job_id: str, summary: Mapping[str, Any] | None) -> str:
    """Use the extracted tenderID as the OneDrive folder name."""
    tender_id = ""
    name = ""
    if isinstance(summary, Mapping):
        if isinstance(summary.get("tenderID"), str):
            tender_id = " ".join(summary["tenderID"].split())
        if isinstance(summary.get("name"), str):
            name = " ".join(summary["name"].split())

    candidate = tender_id or name or job_id

    return _clean_folder_name(candidate) or job_id


def _safe_graph_error(response: requests.Response, operation: str) -> OneDriveError:
    message = f"OneDrive {operation} failed with HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping) and isinstance(error.get("message"), str):
            message = f"{message}: {error['message'][:500]}"
        elif isinstance(payload.get("error_description"), str):
            message = f"{message}: {payload['error_description'][:500]}"
    return OneDriveError(message, response.status_code)


@dataclass
class OneDriveClient:
    client_id: str
    refresh_token: str
    folder_segments: list[str]
    timeout_seconds: int = 120
    session: requests.Session = field(default_factory=requests.Session)

    @classmethod
    def from_environment(cls) -> "OneDriveClient":
        client_id = os.getenv("ONEDRIVE_CLIENT_ID", "").strip()
        refresh_token = os.getenv("ONEDRIVE_REFRESH_TOKEN", "").strip()
        if not client_id:
            raise OneDriveError("ONEDRIVE_CLIENT_ID is required")
        if not refresh_token:
            raise OneDriveError("ONEDRIVE_REFRESH_TOKEN is required")
        folder = os.getenv("ONEDRIVE_FOLDER", "Scraper Function")
        return cls(
            client_id=client_id,
            refresh_token=refresh_token,
            folder_segments=_folder_segments(folder),
            timeout_seconds=_timeout_seconds(),
        )

    def _access_token(self) -> str:
        try:
            response = self.session.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "scope": ONEDRIVE_SCOPE,
                },
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise OneDriveError("Could not connect to Microsoft identity") from exc
        if not response.ok:
            raise _safe_graph_error(response, "authentication")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneDriveError("Microsoft identity returned invalid JSON") from exc
        token = payload.get("access_token") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise OneDriveError("Microsoft identity did not return an access token")
        return token

    def _headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    def _json_request(
        self,
        method: str,
        path: str,
        access_token: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                f"{GRAPH_ROOT}{path}",
                headers=self._headers(access_token),
                timeout=self.timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise OneDriveError("Could not connect to Microsoft Graph") from exc
        if not response.ok:
            raise _safe_graph_error(response, "request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneDriveError("Microsoft Graph returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise OneDriveError("Microsoft Graph returned invalid JSON")
        return payload

    def ensure_output_folder(
        self,
        folder_name: str,
        access_token: str,
    ) -> dict[str, Any]:
        parent = self._json_request("GET", "/me/drive/root", access_token)
        for segment in [*self.folder_segments, folder_name]:
            parent_id = parent.get("id")
            if not isinstance(parent_id, str) or not parent_id:
                raise OneDriveError("OneDrive returned a folder without an id")
            encoded_segment = quote(segment, safe="")
            try:
                parent = self._json_request(
                    "GET",
                    f"/me/drive/items/{parent_id}:/{encoded_segment}",
                    access_token,
                )
            except OneDriveError as exc:
                if exc.status_code != 404:
                    raise
                parent = self._json_request(
                    "POST",
                    f"/me/drive/items/{parent_id}/children",
                    access_token,
                    json={
                        "name": segment,
                        "folder": {},
                        "@microsoft.graph.conflictBehavior": "fail",
                    },
                )
        return parent

    def upload_file(
        self,
        folder_id: str,
        path: Path,
        access_token: str,
    ) -> dict[str, Any]:
        size_bytes = path.stat().st_size
        if size_bytes > MAX_SIMPLE_UPLOAD_BYTES:
            raise OneDriveError(
                f"{path.name} exceeds the 250 MB POC upload limit"
            )
        encoded_name = quote(path.name, safe="")
        try:
            existing_item = self._json_request(
                "GET",
                f"/me/drive/items/{folder_id}:/{encoded_name}",
                access_token,
            )
        except OneDriveError as exc:
            if exc.status_code != 404:
                raise
            existing_item = None

        existing_id = existing_item.get("id") if existing_item else None
        if isinstance(existing_id, str) and existing_id:
            upload_url = f"{GRAPH_ROOT}/me/drive/items/{existing_id}/content"
        else:
            upload_url = (
                f"{GRAPH_ROOT}/me/drive/items/{folder_id}:/{encoded_name}:/content"
            )
        headers = self._headers(access_token)
        headers["Content-Type"] = "application/octet-stream"
        try:
            with path.open("rb") as file_handle:
                response = self.session.put(
                    upload_url,
                    headers=headers,
                    data=file_handle,
                    timeout=self.timeout_seconds,
                )
        except (OSError, requests.RequestException) as exc:
            raise OneDriveError(f"Could not upload {path.name} to OneDrive") from exc
        if not response.ok:
            raise _safe_graph_error(response, f"upload of {path.name}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise OneDriveError("Microsoft Graph returned invalid upload JSON") from exc
        if not isinstance(payload, dict):
            raise OneDriveError("Microsoft Graph returned invalid upload JSON")
        return payload


def _local_output_paths(
    artifacts: Mapping[str, Any],
    downloads: Mapping[str, Any],
) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    candidates: list[Any] = [artifacts.get("summary"), artifacts.get("html")]
    files = downloads.get("files")
    if isinstance(files, list):
        candidates.extend(files)
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        local_path = item.get("localPath")
        if not isinstance(local_path, str):
            continue
        path = Path(local_path).resolve()
        if path.is_file() and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def upload_job_outputs(
    job_id: str,
    artifacts: Mapping[str, Any],
    downloads: Mapping[str, Any],
    *,
    summary: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Upload saved crawl outputs when the optional OneDrive POC is enabled."""
    try:
        if not _enabled():
            return {"status": "disabled", "files": []}
        client = OneDriveClient.from_environment()
        access_token = client._access_token()
        folder_name = tender_folder_name(job_id, summary)
        folder = client.ensure_output_folder(folder_name, access_token)
        folder_id = folder.get("id")
        if not isinstance(folder_id, str) or not folder_id:
            raise OneDriveError("OneDrive returned a job folder without an id")

        files: list[dict[str, Any]] = []
        for path in _local_output_paths(artifacts, downloads):
            try:
                item = client.upload_file(folder_id, path, access_token)
                files.append(
                    {
                        "status": "uploaded",
                        "fileName": path.name,
                        "sizeBytes": path.stat().st_size,
                        "oneDriveId": item.get("id"),
                        "webUrl": item.get("webUrl"),
                    }
                )
            except OneDriveError as exc:
                files.append(
                    {"status": "failed", "fileName": path.name, "error": str(exc)}
                )

        failed = sum(item["status"] == "failed" for item in files)
        return {
            "status": "completed_with_errors" if failed else "completed",
            "folderName": folder_name,
            "folderId": folder_id,
            "folderWebUrl": folder.get("webUrl"),
            "total": len(files),
            "uploaded": len(files) - failed,
            "failed": failed,
            "files": files,
        }
    except OneDriveError as exc:
        return {
            "status": "configuration_error" if "required" in str(exc) else "failed",
            "error": str(exc),
            "files": [],
        }
