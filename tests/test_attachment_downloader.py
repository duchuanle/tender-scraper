import hashlib
from unittest.mock import Mock

from tender_scraper.attachment_downloader import download_attachments


def make_response(content: bytes):
    response = Mock()
    response.url = "https://files.example.com/final/specification.pdf"
    response.headers = {
        "Content-Disposition": 'attachment; filename="Tender Specification.PDF"',
        "Content-Type": "application/pdf",
    }
    response.raise_for_status.return_value = None
    response.iter_content.return_value = [content[:4], content[4:]]
    return response


def test_downloads_attachment_to_job_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("ATTACHMENT_DOWNLOAD_TIMEOUT_SECONDS", raising=False)
    content = b"test-pdf-content"
    response = make_response(content)
    session = Mock()
    session.get.return_value = response
    source_url = "https://files.example.com/download?id=123"

    result = download_attachments(
        "crawl-123",
        [source_url],
        session=session,
        download_root=tmp_path,
    )

    downloaded = result["files"][0]
    saved_path = tmp_path / "crawl-123" / downloaded["fileName"]
    assert result["downloaded"] == 1
    assert downloaded["status"] == "downloaded"
    assert downloaded["fileName"] == "Tender Specification.pdf"
    assert saved_path.read_bytes() == content
    assert downloaded["sha256"] == hashlib.sha256(content).hexdigest()
    session.get.assert_called_once_with(
        source_url,
        allow_redirects=True,
        stream=True,
        timeout=60,
        headers={"Accept": "*/*", "User-Agent": "Mozilla/5.0"},
    )
    response.close.assert_called_once()


def test_does_not_overwrite_existing_attachment(tmp_path):
    content = b"same content"
    response = make_response(content)
    session = Mock()
    session.get.return_value = response
    source_url = "https://files.example.com/download?id=123"

    first = download_attachments(
        "crawl-123", [source_url], session=session, download_root=tmp_path
    )
    second = download_attachments(
        "crawl-123", [source_url], session=session, download_root=tmp_path
    )

    assert first["files"][0]["status"] == "downloaded"
    assert second["files"][0]["status"] == "existing"
    assert second["existing"] == 1


def test_filename_is_stable_when_source_url_changes(tmp_path):
    session = Mock()
    session.get.side_effect = [make_response(b"v1"), make_response(b"v2")]

    first = download_attachments(
        "crawl-1",
        ["https://files.example.com/download?token=aaa"],
        session=session,
        download_root=tmp_path,
    )
    second = download_attachments(
        "crawl-2",
        ["https://files.example.com/download?token=bbb"],
        session=session,
        download_root=tmp_path,
    )

    assert first["files"][0]["fileName"] == "Tender Specification.pdf"
    assert second["files"][0]["fileName"] == "Tender Specification.pdf"


def test_same_filename_from_different_urls_in_one_job_is_disambiguated(tmp_path):
    session = Mock()
    session.get.side_effect = [make_response(b"first"), make_response(b"second")]
    urls = [
        "https://files.example.com/download?id=1",
        "https://files.example.com/download?id=2",
    ]

    result = download_attachments(
        "crawl-123", urls, session=session, download_root=tmp_path
    )

    first, second = result["files"]
    assert result["downloaded"] == 2
    assert first["fileName"] == "Tender Specification.pdf"
    assert second["fileName"].startswith("Tender Specification_")
    assert second["fileName"].endswith(".pdf")
    assert (tmp_path / "crawl-123" / first["fileName"]).read_bytes() == b"first"
    assert (tmp_path / "crawl-123" / second["fileName"]).read_bytes() == b"second"


def test_records_individual_download_failure(tmp_path):
    session = Mock()
    session.get.side_effect = OSError("network unavailable")

    result = download_attachments(
        "crawl-123",
        ["https://files.example.com/a.pdf"],
        session=session,
        download_root=tmp_path,
    )

    assert result["status"] == "completed_with_errors"
    assert result["failed"] == 1
    assert result["files"][0]["status"] == "failed"
