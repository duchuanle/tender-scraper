from attachment_links import extract_attachment_links, is_attachment_url


def test_identifies_common_attachment_extensions_with_query_strings():
    assert is_attachment_url("https://example.com/files/response.DOCX?download=1")
    assert not is_attachment_url("https://example.com/opportunity/123")


def test_identifies_buy_nsw_attachment_host_without_extension():
    assert is_attachment_url(
        "https://nsw-procurement-hub-documents-production.s3.amazonaws.com/"
        "prcOpportunity/download?id=123"
    )


def test_extracts_unique_attachment_links_in_discovery_order():
    documents = [
        {
            "links": [
                "https://example.com/page",
                "https://example.com/a.pdf",
                "https://example.com/a.pdf",
            ]
        },
        {"links": ["https://example.com/b.zip"]},
    ]

    assert extract_attachment_links(documents) == [
        "https://example.com/a.pdf",
        "https://example.com/b.zip",
    ]
