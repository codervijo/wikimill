import asyncio
import random
import os
import re
from datetime import datetime
from playwright.async_api import async_playwright, Page, Browser

SEARCH_QUERY = "hotels near salinas,CA"
GOOGLE_MAPS_URL = "https://www.google.com/maps"
FEED_SELECTOR = 'div[role="feed"]'

def print_with_time(*args, **kwargs):
    now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(now, *args, **kwargs)

# Ensure directories exist
os.makedirs("generated", exist_ok=True)

async def setup_browser(p, headless: bool = True) -> tuple[Browser, Page]:
    """Setup browser with anti-detection settings"""
    browser = await p.chromium.launch(
        headless=headless,
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
            '--disable-extensions',
            '--no-first-run'
        ]
    )
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        viewport={'width': 1366, 'height': 768}
    )
    page = await context.new_page()
    page.set_default_timeout(30000)
    return browser, page

async def human_delay(min_seconds=1, max_seconds=3):
    """Random delay to simulate human behavior"""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))

async def handle_popups(page: Page):
    """Handle cookie consent and location prompts"""
    try:
        # Cookie consent
        cookie_selectors = [
            'button:has-text("Accept all")',
            'button:has-text("I agree")',
            'button:has-text("Accept")',
            'button:has-text("OK")'
        ]
        
        for selector in cookie_selectors:
            try:
                if await page.locator(selector).is_visible(timeout=2000):
                    await page.click(selector)
                    print_with_time("Accepted cookies")
                    await human_delay(1, 2)
                    break
            except:
                continue
        
        # Location prompts
        location_selectors = [
            'button:has-text("Not now")',
            'button:has-text("Don\'t allow")',
            'button:has-text("Block")'
        ]
        
        for selector in location_selectors:
            try:
                if await page.locator(selector).is_visible(timeout=2000):
                    await page.click(selector)
                    print_with_time("Denied location")
                    await human_delay(1, 2)
                    break
            except:
                continue
                
    except Exception as e:
        print_with_time(f"Popup handling error: {e}")

async def navigate_and_search(page: Page, query: str):
    """Navigate to Google Maps and search"""
    print_with_time(f"Searching for: {query}")
    
    # Try direct search URL
    search_url = f"https://www.google.com/maps/search/{query.replace(' ', '+').replace(',', '%2C')}"
    
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await human_delay(2, 4)
        await handle_popups(page)
        
    except Exception as e:
        print_with_time(f"Navigation error: {e}")
        raise

async def wait_for_results(page: Page):
    """Wait for search results"""
    print_with_time("Waiting for results...")
    
    selectors = [FEED_SELECTOR, '[role="article"]', '.Nv2PK']
    
    for selector in selectors:
        try:
            await page.wait_for_selector(selector, timeout=15000)
            print_with_time(f"Results loaded with: {selector}")
            return True
        except:
            continue
    
    print_with_time("No results found")
    return False

async def extract_hotel_names(page: Page):
    """Extract hotel names from results"""
    print_with_time("Extracting hotel names...")
    hotel_names = []
    
    try:
        # Wait a bit more for content to load
        await human_delay(3, 5)
        
        # Try multiple selectors for hotel cards
        card_selectors = [
            f'{FEED_SELECTOR} > div',
            '[role="article"]',
            '.Nv2PK',
            '.bfdHYd'
        ]
        
        cards = []
        for selector in card_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    cards = elements
                    print_with_time(f"Found {len(cards)} cards with selector: {selector}")
                    break
            except:
                continue
        
        if not cards:
            print_with_time("No cards found")
            return hotel_names
        
        # Extract names from each card
        for i, card in enumerate(cards[:30]):  # Limit to first 30
            try:
                # Get all text from the card
                card_text = await card.inner_text()
                
                if not card_text:
                    continue
                
                # Try different name extraction methods
                name = None
                
                # Method 1: Try specific selectors for name
                name_selectors = [
                    '[data-value="Name"]',
                    '.qBF1Pd',
                    '.fontHeadlineSmall',
                    'div[role="heading"]',
                    'h3',
                    'h2'
                ]
                
                for selector in name_selectors:
                    try:
                        name_el = await card.query_selector(selector)
                        if name_el:
                            name = await name_el.inner_text()
                            if name and len(name.strip()) > 2:
                                name = name.strip()
                                break
                    except:
                        continue
                
                # Method 2: Parse from card text
                if not name:
                    lines = [line.strip() for line in card_text.split('\n') if line.strip()]
                    for line in lines:
                        # Skip non-name text
                        if (len(line) > 2 and 
                            not line.startswith('$') and 
                            '★' not in line and
                            not line.replace(',', '').replace('.', '').isdigit() and
                            'review' not in line.lower() and
                            'open' not in line.lower() and
                            'close' not in line.lower() and
                            'hour' not in line.lower() and
                            'mile' not in line.lower() and
                            'km' not in line.lower()):
                            name = line
                            break
                
                # Clean and validate name
                if name:
                    name = name.strip()
                    # Skip obvious non-hotel entries
                    skip_terms = [
                        "Sponsored", "View all", "Hotels", "You've reached the end",
                        "Show more", "See more", "Map", "Satellite", "Traffic",
                        "Search this area", "Hotels in", "More places"
                    ]
                    
                    if not any(term in name for term in skip_terms) and len(name) > 2:
                        if name not in hotel_names:  # Avoid duplicates
                            hotel_names.append(name)
                            print_with_time(f"Found: {name}")
                
            except Exception as e:
                print_with_time(f"Error processing card {i}: {e}")
                continue
        
    except Exception as e:
        print_with_time(f"Extraction error: {e}")
    
    return hotel_names

async def scroll_for_more_results(page: Page, max_scrolls=5):
    """Scroll to load more results"""
    try:
        feed = await page.query_selector(FEED_SELECTOR)
        if not feed:
            return
        
        for i in range(max_scrolls):
            # Scroll down in the feed
            await page.evaluate(
                """(feed) => {
                    feed.scrollTo({ top: feed.scrollHeight, behavior: "smooth" });
                }""", 
                feed
            )
            await human_delay(2, 3)
            
    except Exception as e:
        print_with_time(f"Scroll error: {e}")

async def main():
    print_with_time("Starting hotel name scraper...")
    browser = None
    
    try:
        async with async_playwright() as p:
            browser, page = await setup_browser(p, headless=True)
            
            await navigate_and_search(page, SEARCH_QUERY)
            
            if not await wait_for_results(page):
                print_with_time("No results found")
                return
            
            # Scroll to load more results
            await scroll_for_more_results(page)
            
            # Extract hotel names
            hotel_names = await extract_hotel_names(page)
            
            # Print results
            print_with_time("\n" + "="*50)
            print_with_time("HOTEL NAMES FOUND:")
            print_with_time("="*50)
            
            if hotel_names:
                for i, name in enumerate(hotel_names, 1):
                    print(f"{i:2d}. {name}")
                
                print_with_time(f"\nTotal hotels found: {len(hotel_names)}")
            else:
                print_with_time("No hotel names found")
            
            print_with_time("="*50)
            
    except Exception as e:
        print_with_time(f"Script failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if browser:
            await browser.close()
            print_with_time("Browser closed")

if __name__ == "__main__":
    asyncio.run(main())