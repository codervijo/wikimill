import pytest
from playwright.sync_api import sync_playwright

def test_example():
    with sync_playwright() as p:
        print("Launching Chromium in headed mode...")
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        page.goto("https://example.com")
        print("Page title:", page.title())
        assert "Example Domain" in page.title()
        browser.close()

if __name__ == "__main__":
    test_example()
