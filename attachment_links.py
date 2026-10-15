"""Extract downloadable attachment URLs from Firecrawl link results."""

from __future__ import annotations

from html import unescape
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


ATTACHMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docm",
    ".docx",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rar",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsb",
    ".xlsm",
    ".xlsx",
    ".xml",
    ".zip",
}

# buy NSW currently stores opportunity attachments on this host. Treat every
# discovered link on it as an attachment, including URLs without an extension.
ATTACHMENT_HOSTS = {
    "nsw-procurement-hub-documents-production.s3.amazonaws.com",
}


def is_attachment_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False

    url = unescape(value).strip()
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return False
    if hostname in ATTACHMENT_HOSTS:
        return True

    path = unquote(parsed.path).lower()
    return any(path.endswith(extension) for extension in ATTACHMENT_EXTENSIONS)


def extract_attachment_links(documents: Any) -> list[str]:
    """Return unique attachment URLs in Firecrawl discovery order."""
    if not isinstance(documents, list):
        return []

    links: list[str] = []
    seen: set[str] = set()
    for document in documents:
        if not isinstance(document, dict):
            continue
        document_links: Iterable[Any] = document.get("links", [])
        if not isinstance(document_links, list):
            continue
        for value in document_links:
            if not is_attachment_url(value):
                continue
            url = unescape(value).strip()
            if url in seen:
                continue
            seen.add(url)
            links.append(url)
    return links
