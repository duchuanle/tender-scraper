from pathlib import Path
from unittest.mock import Mock, patch

import onedrive_client


def test_upload_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ONEDRIVE_ENABLED", raising=False)

    result = onedrive_client.upload_job_outputs("crawl-123", {}, {})

    assert result == {"status": "disabled", "files": []}


def test_missing_personal_onedrive_configuration_is_reported(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_ENABLED", "true")
    monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("ONEDRIVE_REFRESH_TOKEN", raising=False)

    result = onedrive_client.upload_job_outputs("crawl-123", {}, {})

    assert result["status"] == "configuration_error"
    assert result["files"] == []


@patch("onedrive_client.OneDriveClient.from_environment")
def test_uploads_summary_html_and_downloaded_attachments(
    from_environment,
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("ONEDRIVE_ENABLED", "true")
    summary = tmp_path / "summary.json"
    html = tmp_path / "website.html"
    attachment = tmp_path / "specification.pdf"
    summary.write_text("{}", encoding="utf-8")
    html.write_text("<html></html>", encoding="utf-8")
    attachment.write_bytes(b"pdf")

    client = Mock()
    client._access_token.return_value = "access-token"
    client.ensure_output_folder.return_value = {
        "id": "folder-id",
        "webUrl": "https://onedrive.example/job-folder",
    }
    client.upload_file.side_effect = [
        {"id": "summary-id", "webUrl": "https://onedrive.example/summary"},
        {"id": "html-id", "webUrl": "https://onedrive.example/html"},
        {"id": "attachment-id", "webUrl": "https://onedrive.example/file"},
    ]
    from_environment.return_value = client

    result = onedrive_client.upload_job_outputs(
        "crawl-123",
        {
            "summary": {"localPath": str(summary)},
            "html": {"localPath": str(html)},
        },
        {"files": [{"localPath": str(attachment)}]},
        summary={
            "tenderID": "NSWSES001090 - Request For Information",
            "name": "Request For Information",
        },
    )

    assert result["status"] == "completed"
    assert result["folderWebUrl"] == "https://onedrive.example/job-folder"
    assert result["total"] == 3
    assert result["uploaded"] == 3
    assert result["failed"] == 0
    assert [item["fileName"] for item in result["files"]] == [
        "summary.json",
        "website.html",
        "specification.pdf",
    ]
    assert result["folderName"] == "NSWSES001090 - Request For Information"
    client.ensure_output_folder.assert_called_once_with(
        "NSWSES001090 - Request For Information",
        "access-token",
    )
    assert client.upload_file.call_count == 3


def test_tender_folder_name_uses_extracted_tender_id_and_sanitizes_it():
    assert onedrive_client.tender_folder_name(
        "crawl-123",
        {"tenderID": "RFT/123 - Cloud: hosting?", "name": "Ignored name"},
    ) == "RFT-123 - Cloud- hosting-"
    assert onedrive_client.tender_folder_name(
        "crawl-123",
        {
            "tenderID": "NSWSES001090 - Request For Information",
            "name": "A different extracted title",
        },
    ) == "NSWSES001090 - Request For Information"
    assert onedrive_client.tender_folder_name(
        "crawl-123",
        {"tenderID": "", "name": "Fallback opportunity name"},
    ) == "Fallback opportunity name"
    assert onedrive_client.tender_folder_name("crawl-123", None) == "crawl-123"


def test_upload_file_replaces_existing_file_by_id(tmp_path: Path):
    path = tmp_path / "summary.json"
    path.write_text('{"version": 2}', encoding="utf-8")
    session = Mock()
    response = Mock(ok=True)
    response.json.return_value = {"id": "existing-file-id", "name": "summary.json"}
    session.put.return_value = response
    client = onedrive_client.OneDriveClient(
        client_id="client-id",
        refresh_token="refresh-token",
        folder_segments=["Scraper Function"],
        session=session,
    )
    client._json_request = Mock(return_value={"id": "existing-file-id"})

    result = client.upload_file("folder-id", path, "access-token")

    assert result["id"] == "existing-file-id"
    assert session.put.call_args.args[0] == (
        "https://graph.microsoft.com/v1.0/me/drive/items/"
        "existing-file-id/content"
    )


def test_upload_file_creates_file_when_name_does_not_exist(tmp_path: Path):
    path = tmp_path / "website.html"
    path.write_text("<html></html>", encoding="utf-8")
    session = Mock()
    response = Mock(ok=True)
    response.json.return_value = {"id": "new-file-id", "name": "website.html"}
    session.put.return_value = response
    client = onedrive_client.OneDriveClient(
        client_id="client-id",
        refresh_token="refresh-token",
        folder_segments=["Scraper Function"],
        session=session,
    )
    client._json_request = Mock(
        side_effect=onedrive_client.OneDriveError("not found", status_code=404)
    )

    client.upload_file("folder-id", path, "access-token")

    assert session.put.call_args.args[0] == (
        "https://graph.microsoft.com/v1.0/me/drive/items/"
        "folder-id:/website.html:/content"
    )


def test_invalid_onedrive_folder_is_rejected(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_ENABLED", "true")
    monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "client-id")
    monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setenv("ONEDRIVE_FOLDER", "Scraper:Function")

    result = onedrive_client.upload_job_outputs("crawl-123", {}, {})

    assert result["status"] == "failed"
    assert "invalid OneDrive name" in result["error"]
