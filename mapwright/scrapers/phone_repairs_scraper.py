import asyncio
import csv
import random
import os
from playwright.async_api import async_playwright, Page, Browser

SEARCH_QUERY = "phone repair near salinas,CA"
GOOGLE_MAPS_URL = "https://www.google.com/maps"
CSV_FILENAME = "output/phone_repairs.csv"
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

async def scroll_and_scrape_feed(
    page: Page,
    scrape_card_details_fn,
    max_scrolls: int = 30
) -> list[dict]:
    """
    Scrolls the results feed and scrapes details for each card using the provided scraping function.
    Stops only when a card with name "You've reached the end of the list." is found.
    scrape_card_details_fn: function(page: Page, card_index: int, feed_selector: str) -> dict
    """
    print("Scrolling results feed and scraping details...")
    all_results = []
    seen_names = set()
    reached_end = False
    scroll_num = 0
    while not reached_end and scroll_num < max_scrolls:
        feed = await page.query_selector(FEED_SELECTOR)
        if not feed:
            print("No results feed found during scrolling.")
            break
        cards = await feed.query_selector_all(':scope > div')
        count = len(cards)
        print(f"Scroll {scroll_num+1}: {count} cards found.")
        for i in range(count):
            result = await scrape_card_details_fn(page, i, FEED_SELECTOR)
            name = result.get("name", "")
            # Avoid duplicates by name and address
            key = (name, result.get("address", ""))
            if key not in seen_names:
                all_results.append(result)
                seen_names.add(key)
            if name == "You've reached the end of the list.":
                reached_end = True
        if reached_end:
            print("Detected end of the list card. Stopping scroll.")
            break
        await page.evaluate(
            f'''
            (feed) => {{
                feed.scrollTo({{ top: feed.scrollHeight, behavior: "smooth" }});
            }}
            ''',
            feed
        )
        await asyncio.sleep(random.uniform(2.0, 4.5))  # Random wait to avoid bot detection
        scroll_num += 1
    return all_results

async def scrape_shop_details(page: Page):
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

async def scrape_shop_card(page: Page, card_index: int, feed_selector: str) -> dict:
    """
    Scrapes details for a single shop card at the given index.
    """
    i = card_index
    name = ""
    address = ""
    website = ""
    phone = ""
    email = ""
    card_selector = f'{feed_selector} > div:nth-child({i+1})'
    card = await page.query_selector(card_selector)
    if not card:
        print(f"Card {i+1}: Could not find card element, skipping.")
        return {
            "name": "Unknown",
            "address": "",
            "website": "",
            "phone": "",
            "email": ""
        }
    try:
        name_el = await card.query_selector('.qBF1Pd.fontHeadlineSmall')
        if name_el:
            name = (await name_el.inner_text()).strip()
        if not name or name in ["Sponsored", "View all", "Hotels", "", "You've reached the end of the list."]:
            name = name or "Unknown"
            return {
                "name": name,
                "address": "",
                "website": "",
                "phone": "",
                "email": ""
            }

        print(f"Card {i+1}: {name}")
        try:
            await card.click()
            await asyncio.sleep(random.uniform(1.5, 3.5))
            await page.wait_for_selector('div[role="dialog"], div[role="main"]', timeout=10000)
            address, website, phone, email = await scrape_shop_details(page)
        except Exception as e:
            print(f"Error scraping details for card {i+1}: {e}")

        print(f"  Address: {address}")
        print(f"  Website: {website}")
        print(f"  Phone: {phone}")
        print(f"  Email: {email}")
        try:
            close_btn = await page.query_selector('button[aria-label="Back"], button[aria-label="Close"], button[aria-label="Close side panel"]')
            if close_btn:
                await close_btn.click()
                await asyncio.sleep(random.uniform(1.0, 2.5))
                await page.wait_for_selector(feed_selector, timeout=10000)
        except Exception:
            pass
        return {
            "name": name,
            "address": address,
            "website": website,
            "phone": phone,
            "email": email
        }
    except Exception as e:
        print(f"Error scraping card {i+1}: {e}")
        return {
            "name": name or "Unknown",
            "address": "",
            "website": "",
            "phone": "",
            "email": ""
        }

def write_shops_to_csv(shops: list[dict], filename: str):
    print("Writing results to CSV...")
    with open(filename, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["name", "address", "website", "phone", "email"])
        writer.writeheader()
        for shop in shops:
            writer.writerow(shop)
    print(f"Saved {len(shops)} phone repair shops to {filename}.")

async def main():
    print("Starting Playwright script for phone repairs...")
    browser = None
    try:
        async with async_playwright() as p:
            browser, page = await setup_browser(p, headless=False)
            await navigate_and_search(page, SEARCH_QUERY)
            await wait_for_results(page)
            await minimize_map(page)
            await dump_html(page, "maps_results.html")
            shops = await scroll_and_scrape_feed(page, scrape_shop_card)
            write_shops_to_csv(shops, CSV_FILENAME)
    except Exception as e:
        print(f"Script failed: {e}")
    finally:
        if browser:
            await browser.close()
            print("Browser closed. Script finished.")

if __name__ == "__main__":
    asyncio.run(main())
