from importlib import import_module


def test_runtime_modules_are_importable_from_package():
    module_names = (
        "app",
        "attachment_downloader",
        "attachment_links",
        "firecrawl_client",
        "onedrive_client",
        "settings",
        "tender_artifacts",
    )

    imported = {
        name: import_module(f"tender_scraper.{name}") for name in module_names
    }

    assert imported["app"].app.title == "Tender Scraper"
