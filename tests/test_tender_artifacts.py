import json

from tender_artifacts import (
    TENDER_FIELDS,
    extract_tender_summary,
    save_crawl_artifacts,
    select_primary_document,
)


def test_selects_target_document_and_normalizes_summary_url():
    documents = [
        {
            "json": {"name": "Other page"},
            "metadata": {"sourceURL": "https://example.com/other"},
        },
        {
            "json": {
                "name": "  Tender   title ",
                "category": ["Education", "Technology"],
                "url": "[bad markdown](https://wrong.example)",
            },
            "html": "<html>Tender</html>",
            "metadata": {"sourceURL": "https://example.com/tender/"},
        },
    ]

    document = select_primary_document(documents, "https://example.com/tender")
    summary = extract_tender_summary(document, "https://example.com/tender")

    assert summary is not None
    assert tuple(summary) == TENDER_FIELDS
    assert summary["name"] == "Tender title"
    assert summary["category"] == "Education, Technology"
    assert summary["agency"] == ""
    assert summary["url"] == "https://example.com/tender"


def test_saves_summary_and_html_beside_attachments(tmp_path):
    summary = {field: f"value-{field}" for field in TENDER_FIELDS}

    artifacts = save_crawl_artifacts(
        "crawl-123",
        summary,
        "<html><body>Tender</body></html>",
        download_root=tmp_path,
    )

    output_folder = tmp_path / "crawl-123"
    assert artifacts["status"] == "completed"
    assert artifacts["summary"]["status"] == "saved"
    assert artifacts["html"]["status"] == "saved"
    assert json.loads((output_folder / "summary.json").read_text("utf-8")) == summary
    assert (output_folder / "website.html").read_text("utf-8") == (
        "<html><body>Tender</body></html>"
    )

    repeated = save_crawl_artifacts(
        "crawl-123",
        summary,
        "<html><body>Tender</body></html>",
        download_root=tmp_path,
    )
    assert repeated["summary"]["status"] == "existing"
    assert repeated["html"]["status"] == "existing"
