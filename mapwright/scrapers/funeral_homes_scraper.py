import asyncio
import csv
import random
import os
from playwright.async_api import async_playwright, Page, Browser

SEARCH_QUERY = "funeral homes near salinas,CA"
GOOGLE_MAPS_URL = "https://www.google.com/maps"
CSV_FILENAME = "output/funeral_homes.csv"
FEED_SELECTOR = 'div[role="feed"]'

# Ensure the generated/ directory exists
os.makedirs(os.path.join(os.path.dirname(__file__), "../generated"), exist_ok=True)

async def setup_browser(p, headless: bool = False) -> tuple[Browser, Page]:
    browser = await p.chromium.launch(headless=headless)
    page = await browser.new_page()
    return browser, page

async def handle_cookies(page: Page):
    try:
        print("Checking for cookie consent popup...")
        await page.click('button:has-text("Accept all")', timeout=5000)
        print("Accepted cookies.")
    except Exception:
        print("No cookie consent popup found or could not click.")

async def navigate_and_search(page: Page, query: str):
    print(f"Navigating to {GOOGLE_MAPS_URL} ...")
    await page.goto(GOOGLE_MAPS_URL, wait_until="load")
    await handle_cookies(page)
    print("Saving initial screenshot and HTML (before search)...")
    await page.screenshot(path="generated/maps_initial.png")
    html_content = await page.content()
    with open("generated/maps_initial.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("Saved generated/maps_initial.png and generated/maps_initial.html.")

    search_box_selectors = [
        'input[aria-label="Search Google Maps"]',
        'input#searchboxinput',
        'input[placeholder*="Search"]'
    ]
    found = False
    for selector in search_box_selectors:
        print(f"Trying search box selector: {selector}")
        try:
            await page.wait_for_selector(selector, timeout=10000)
            await page.fill(selector, query)
            await page.keyboard.press("Enter")
            print(f"Successfully found and used selector: {selector}")
            found = True
            break
        except Exception as e:
            print(f"Selector failed: {selector} ({e})")
    if not found:
        raise RuntimeError("Could not find the search box. See maps_initial.png and maps_initial.html for debugging.")

async def wait_for_results(page: Page):
    print("Waiting for results to load (div[role='feed'])...")
    try:
        await page.wait_for_selector(FEED_SELECTOR, timeout=20000)
        print("Results container found.")
    except Exception as e:
        print("ERROR: Could not find results container. Try inspecting the page manually.")
        await page.screenshot(path="generated/maps_after_search_error.png")
        html_content = await page.content()
        with open("generated/maps_after_search_error.html", "w", encoding="utf-8") as f:
            f.write(html_content)
        print("Saved generated/maps_after_search_error.png and generated/maps_after_search_error.html for debugging.")
        raise

async def minimize_map(page: Page):
    print("Minimizing map and showing only the list...")
    await page.evaluate("""
    const mapSelectors = [
        'div[role="region"][aria-label*="Map"]',
        'div.section-layout.section-scrollbox.scrollable-y.scrollable-show',
        'div#scene',
        'div.widget-scene',
        'div#pane div[role="main"] > div > div > div:not([role="feed"])'
    ];
    for (const sel of mapSelectors) {
        const el = document.querySelector(sel);
        if (el) {
            el.style.display = "none";
        }
    }
    const listContainer = document.querySelector('div[role="feed"]');
    if (listContainer) {
        listContainer.style.width = "100vw";
    }
    """)
    print("Map minimized, only list should be visible.")

async def dump_html(page: Page, filename: str):
    # Always write HTML debug files to generated/
    base = os.path.basename(filename)
    out_path = os.path.join(os.path.dirname(__file__), "../generated", base)
    html_content = await page.content()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Saved {out_path}.")

async def scroll_results_feed(page: Page, max_scrolls: int = 15):
    print("Scrolling results feed to load more funeral homes...")
    last_count = 0
    for scroll_num in range(max_scrolls):
        feed = await page.query_selector(FEED_SELECTOR)
        if not feed:
            print("No results feed found during scrolling.")
            break
        cards = await feed.query_selector_all(':scope > div')
        count = len(cards)
        print(f"Scroll {scroll_num+1}: {count} cards found.")

        # Check for "You've reached the end of the list." in card names
        reached_end = False
        for i, card in enumerate(cards):
            try:
                name_el = await card.query_selector('.qBF1Pd.fontHeadlineSmall')
                if name_el:
                    name = (await name_el.inner_text()).strip()
                    if name == "You've reached the end of the list.":
                        print(f"Detected end of the list card at index {i+1}. Stopping scroll.")
                        reached_end = True
                        break
            except Exception:
                continue
        if reached_end:
            break

        if count == last_count:
            print("No new cards loaded after scrolling. Stopping.")
            break
        last_count = count
        await page.evaluate(
            f'''
            (feed) => {{
                feed.scrollTo({{ top: feed.scrollHeight, behavior: "smooth" }});
            }}
            ''',
            feed
        )
        await asyncio.sleep(random.uniform(2.0, 4.5))  # Random wait to avoid bot detection

async def scrape_funeral_home_details(page: Page):
    address = ""
    website = ""
    phone = ""
    email = ""
    try:
        address_el = await page.query_selector('button[data-item-id="address"] .fontBodyMedium, .Io6YTe.fontBodyMedium')
        if address_el:
            address = (await address_el.inner_text()).strip()
    except Exception:
        pass
    try:
        website_el = await page.query_selector('a[data-item-id="authority"]')
        if website_el:
            website = (await website_el.get_attribute("href")).strip()
    except Exception:
        pass
    try:
        phone_el = await page.query_selector('button[data-item-id="phone"] .fontBodyMedium, .Io6YTe.fontBodyMedium')
        if phone_el:
            phone = (await phone_el.inner_text()).strip()
    except Exception:
        pass
    try:
        # Look for an <a> tag with href starting with "mailto:"
        email_el = await page.query_selector('a[href^="mailto:"]')
        if email_el:
            href = await email_el.get_attribute("href")
            if href and href.startswith("mailto:"):
                email = href.replace("mailto:", "").split("?")[0].strip()
    except Exception:
        pass
    return address, website, phone, email

async def scrape_funeral_homes_from_feed(page: Page) -> list[dict]:
    print("Scraping funeral home details from results...")
    funeral_homes = []
    feed = await page.query_selector(FEED_SELECTOR)
    if not feed:
        print("No results feed found for scraping.")
        return funeral_homes

    cards = await feed.query_selector_all(':scope > div')
    num_cards = len(cards)
    for i in range(num_cards):
        name = ""
        address = ""
        website = ""
        phone = ""
        email = ""
        card = cards[i]
        try:
            name_el = await card.query_selector('.qBF1Pd.fontHeadlineSmall')
            if name_el:
                name = (await name_el.inner_text()).strip()
            if not name or name in ["Sponsored", "View all", "Funeral Homes", "", "You've reached the end of the list."]:
                name = name or "Unknown"
                funeral_homes.append({
                    "name": name,
                    "address": "",
                    "website": "",
                    "phone": "",
                    "email": ""
                })
                continue

            print(f"Card {i+1}: {name}")
            # Click the card to open details
            try:
                # Re-query the card to avoid stale element reference
                feed = await page.query_selector(FEED_SELECTOR)
                cards = await feed.query_selector_all(':scope > div')
                fresh_card = cards[i]
                await fresh_card.scroll_into_view_if_needed()
                await asyncio.sleep(random.uniform(0.5, 1.0))  # Small wait after scrolling
                await fresh_card.click()
                await asyncio.sleep(random.uniform(1.5, 3.5))  # Random wait after clicking card
                # Wait for details pane to load
                await page.wait_for_selector('div[role="dialog"], div[role="main"]', timeout=10000)
                address, website, phone, email = await scrape_funeral_home_details(page)
            except Exception as e:
                print(f"Error scraping details for card {i+1}: {e}")
                # address, website, phone, email remain empty

            funeral_homes.append({
                "name": name,
                "address": address,
                "website": website,
                "phone": phone,
                "email": email
            })
            print(f"  Address: {address}")
            print(f"  Website: {website}")
            print(f"  Phone: {phone}")
            print(f"  Email: {email}")
            # Try to close the details pane (click back or close)
            try:
                close_btn = await page.query_selector('button[aria-label="Back"], button[aria-label="Close"], button[aria-label="Close side panel"]')
                if close_btn:
                    await close_btn.click()
                    await asyncio.sleep(random.uniform(1.0, 2.5))  # Random wait after closing details
                    # Wait for the feed to reappear before continuing
                    await page.wait_for_selector(FEED_SELECTOR, timeout=10000)
            except Exception:
                pass
        except Exception as e:
            print(f"Error scraping card {i+1}: {e}")
            # If we couldn't even get the name, still write a row with "Unknown"
            funeral_homes.append({
                "name": name or "Unknown",
                "address": "",
                "website": "",
                "phone": "",
                "email": ""
            })
    return funeral_homes

def write_funeral_homes_to_csv(funeral_homes: list[dict], filename: str):
    print("Writing results to CSV...")
    with open(filename, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["name", "address", "website", "phone", "email"])
        writer.writeheader()
        for home in funeral_homes:
            writer.writerow(home)
    print(f"Saved {len(funeral_homes)} funeral homes to {filename}.")

async def main():
    print("Starting Playwright script...")
    browser = None
    try:
        async with async_playwright() as p:
            browser, page = await setup_browser(p, headless=False)
            await navigate_and_search(page, SEARCH_QUERY)
            await wait_for_results(page)
            await minimize_map(page)
            await dump_html(page, "maps_results.html")
            await scroll_results_feed(page)
            funeral_homes = await scrape_funeral_homes_from_feed(page)
            write_funeral_homes_to_csv(funeral_homes, CSV_FILENAME)
    except Exception as e:
        print(f"Script failed: {e}")
    finally:
        if browser:
            await browser.close()
            print("Browser closed. Script finished.")

if __name__ == "__main__":
    asyncio.run(main())
