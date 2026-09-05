from pathlib import Path


def test_playwright_is_declared_without_browser_download_hook():
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "playwright>=" in requirements
    assert "playwright install" not in requirements


def test_playwright_python_api_is_importable():
    from playwright.sync_api import sync_playwright

    assert callable(sync_playwright)
