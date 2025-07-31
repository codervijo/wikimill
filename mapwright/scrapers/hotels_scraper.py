import asyncio
import csv
import random
import os
import re
import json
from datetime import datetime
from playwright.async_api import async_playwright, Page, Browser

SEARCH_QUERY = "hotels near salinas,CA"
GOOGLE_MAPS_URL = "https://www.google.com/maps"
CSV_FILENAME = "output/hotels.csv"
JSON_FILENAME = "output/hotels.json"

# Google Maps UI elements to skip
SKIP_UI_ELEMENTS = [
    "vacation rentals are also available for your dates",
    "vacation rentals are also available",
    "sort by",
    "share",
    "results",
    "more places",
    "search this area",
    "view all",
    "sponsored",
    "hotels",
    "show more results",
    "load more",
    "see more results",
    "directions",
    "save",
    "nearby",
    "call",
    "website"
]

def print_with_time(*args, **kwargs):
    now = datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")
    print(now, *args, **kwargs)

# Ensure directories exist
os.makedirs("output", exist_ok=True)
os.makedirs("generated", exist_ok=True)

def is_valid_business_name(name: str) -> bool:
    """Check if the extracted text is a valid business name"""
    if not name or len(name.strip()) < 3:
        return False
    
    name_lower = name.lower().strip()
    
    # Skip UI elements
    for skip_term in SKIP_UI_ELEMENTS:
        if skip_term in name_lower:
            return False
    
    # Skip if it's just numbers, symbols, or very short
    if name.strip().isdigit():
        return False
    
    # Skip common non-business patterns
    non_business_patterns = [
        r'^\d+\s*(km|mi|miles|kilometers)$',  # Distance indicators
        r'^\d+\s*min$',  # Time indicators
        r'^[★☆]+$',  # Just stars
        r'^\$+$',  # Just dollar signs
        r'^open|closed$',  # Status indicators
        r'^\d+:\d+\s*(am|pm)?$',  # Time formats
        r'^rating:\s*\d',  # Rating text
        r'^\(\d+\)$',  # Just review numbers
    ]
    
    for pattern in non_business_patterns:
        if re.match(pattern, name_lower):
            return False
    
    # Must contain at least one letter
    if not re.search(r'[a-zA-Z]', name):
        return False
    
    # Skip if it's too generic
    generic_terms = ["hotel", "motel", "restaurant", "business", "place", "location"]
    if name_lower in generic_terms:
        return False
    
    return True

async def setup_browser(p, headless: bool = False) -> tuple[Browser, Page]:
    """Setup browser with anti-detection settings"""
    browser = await p.chromium.launch(
        headless=headless,
        slow_mo=500,  # Slower interactions
        args=[
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--disable-blink-features=AutomationControlled',
            '--disable-extensions'
        ]
    )
    context = await browser.new_context(
        user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        viewport={'width': 1366, 'height': 768}
    )
    page = await context.new_page()
    page.set_default_timeout(60000)
    return browser, page

async def human_delay(min_seconds=1, max_seconds=3):
    """Random delay to simulate human behavior"""
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))

async def navigate_and_search(page: Page, query: str):
    """Navigate and search with better error handling"""
    print_with_time(f"Navigating to {GOOGLE_MAPS_URL}...")
    
    try:
        await page.goto(GOOGLE_MAPS_URL, wait_until="domcontentloaded", timeout=30000)
        print_with_time("✓ Page loaded successfully")
    except Exception as e:
        print_with_time(f"Load failed, trying networkidle: {e}")
        await page.goto(GOOGLE_MAPS_URL, wait_until="networkidle", timeout=60000)
    
    await human_delay(3, 5)
    
    # Handle popups
    try:
        popup_selectors = [
            'button:has-text("Accept all")',
            'button:has-text("I agree")',
            'button:has-text("OK")',
            'button:has-text("Got it")'
        ]
        for selector in popup_selectors:
            try:
                await page.click(selector, timeout=3000)
                print_with_time(f"Clicked popup: {selector}")
                break
            except:
                continue
    except:
        pass
    
    # Save initial state
    await page.screenshot(path="generated/maps_initial.png")
    
    # Search with multiple strategies
    search_selectors = [
        'input#searchboxinput',
        'input[aria-label*="Search"]',
        'input[role="combobox"]'
    ]
    
    searched = False
    for selector in search_selectors:
        try:
            print_with_time(f"Trying search with: {selector}")
            await page.wait_for_selector(selector, timeout=10000)
            
            search_box = page.locator(selector).first
            await search_box.click()
            await human_delay(1, 2)
            
            # Clear and type
            await search_box.fill("")
            await human_delay(0.5, 1)
            
            # Type character by character
            for char in query:
                await page.keyboard.type(char)
                await asyncio.sleep(random.uniform(0.05, 0.1))
            
            await human_delay(1, 2)
            await page.keyboard.press("Enter")
            
            print_with_time(f"✓ Search completed with: {selector}")
            searched = True
            break
            
        except Exception as e:
            print_with_time(f"Search failed with {selector}: {e}")
    
    if not searched:
        print_with_time("All search methods failed, trying direct URL...")
        direct_url = f"https://www.google.com/maps/search/{query.replace(' ', '+')}"
        await page.goto(direct_url, timeout=60000)

async def wait_for_results(page: Page):
    """Wait for results and return the best selector for individual business cards"""
    print_with_time("Waiting for search results...")
    
    # First wait for the feed container
    await page.wait_for_selector('div[role="feed"]', timeout=15000)
    
    # Now look for individual business listing selectors within the feed
    business_card_selectors = [
        'div[role="feed"] > div > div[jsaction]',  # Common business card pattern
        'div[role="feed"] > div[data-result-index]',  # Indexed results
        'div[role="feed"] a[href*="/place/"]',  # Place links
        'div[role="feed"] div[data-cid]',  # Business with CID
        'div[role="feed"] > div > div[data-result-ad-cid]',  # Ad results
        'div[role="feed"] > div:has(a[href*="/place/"])',  # Divs containing place links
    ]
    
    best_selector = None
    max_count = 0
    
    for selector in business_card_selectors:
        try:
            await page.wait_for_selector(selector, timeout=5000)
            count = await page.locator(selector).count()
            print_with_time(f"Found {count} results with: {selector}")
            
            if count > max_count:
                max_count = count
                best_selector = selector
                
        except Exception as e:
            print_with_time(f"Selector {selector} failed: {e}")
    
    if best_selector and max_count > 0:
        print_with_time(f"✓ Using best selector: {best_selector} ({max_count} results)")
        return best_selector
    
    # Fallback: try to find any clickable business elements
    fallback_selectors = [
        'div[role="feed"] [data-cid]',
        'div[role="feed"] a[data-cid]',
        '[jsaction*="mouseover"]',
    ]
    
    for selector in fallback_selectors:
        try:
            count = await page.locator(selector).count()
            if count > 0:
                print_with_time(f"✓ Fallback selector found: {selector} ({count} results)")
                return selector
        except:
            continue
    
    # Save debug info
    await page.screenshot(path="generated/no_results_debug.png")
    with open("generated/no_results_debug.html", "w", encoding="utf-8") as f:
        f.write(await page.content())
    
    print_with_time("Debug info saved. Page content preview:")
    page_text = await page.inner_text('body')
    print_with_time(page_text[:500] + "...")
    
    raise Exception("No business card results found")

def extract_business_name_from_text(card_text: str) -> str:
    """Extract business name from card text, filtering out UI elements"""
    lines = [line.strip() for line in card_text.split('\n') if line.strip()]
    
    # Look for the business name in the first few lines
    for line in lines[:5]:  # Check first 5 lines only
        # Clean the line
        cleaned_line = re.sub(r'\s+', ' ', line).strip()
        
        # Skip lines that are clearly not business names
        if (len(cleaned_line) < 3 or 
            cleaned_line.startswith('$') or 
            '★' in cleaned_line or
            cleaned_line.isdigit() or
            'review' in cleaned_line.lower() or
            'open' in cleaned_line.lower() or
            'closed' in cleaned_line.lower() or
            'km' in cleaned_line.lower() or
            'mi' in cleaned_line.lower() or
            re.match(r'^\d+:\d+', cleaned_line) or  # Time format
            re.match(r'^\(\d+\)', cleaned_line)):   # Review count
            continue
        
        # Check if it's a valid business name
        if is_valid_business_name(cleaned_line):
            return cleaned_line
    
    return ""

async def extract_hotel_info_from_card(page: Page, card_index: int, results_selector: str):
    """Extract hotel info using index-based selection to avoid stale elements"""
    info = {
        "name": "",
        "rating": None,
        "reviews": None,
        "price": "",
        "address": "",
        "phone": "",
        "website": "",
        "category": ""
    }
    
    try:
        # Use index-based selector to avoid stale elements
        # This uses Playwright's locator().nth(index) pattern, not invalid CSS selectors.
        card_locator = page.locator(results_selector).nth(card_index)
        try:
            await card_locator.wait_for(state="attached", timeout=5000)
        except Exception as e:
            print_with_time(f"    Error waiting for card {card_index + 1} with locator().nth({card_index}): {e}")
            return None
        
        # Check if this card actually contains business info by looking for place links
        has_place_link = await card_locator.locator('a[href*="/place/"]').count() > 0
        if not has_place_link:
            print_with_time(f"  Card {card_index + 1}: No place link found, checking content...")
        
        # Get all text from the card for parsing
        try:
            card_text = await card_locator.inner_text()
            print_with_time(f"  Card {card_index + 1} text preview: {card_text[:80]}...")
        except Exception as e:
            print_with_time(f"    Error getting inner text for card {card_index + 1}: {e}")
            card_text = ""
        
        if not card_text.strip():
            print_with_time(f"  Card {card_index + 1}: Empty text, skipping")
            return None
        
        # Check if this is actually a business listing or just page chrome
        if len(card_text) < 20:  # Very short text, likely not a business
            print_with_time(f"  Card {card_index + 1}: Text too short, likely page element")
            return None
        
        # Look for business indicators in the text
        business_indicators = ['★', 'reviews', 'rating']
    except Exception as e:
        print_with_time(f"    Error extracting detailed info for card {card_index + 1} (locator().nth({card_index})): {e}")
        return None
    
    return info

async def extract_detailed_info(page: Page):
    """Extract detailed info from opened hotel panel"""
    info = {
        "address": "",
        "phone": "",
        "website": ""
    }
    
    try:
        # Wait for panel to load
        await page.wait_for_selector('div[role="main"], div[data-value="main"]', timeout=8000)
        
        # Address
        address_selectors = [
            'button[data-item-id="address"]',
            '[data-item-id="address"]',
            'button[jsaction*="address"]',
            '.rogA2c .Io6YTe'  # Common address class
        ]
        
        for selector in address_selectors:
            try:
                addr_elem = await page.query_selector(selector)
                if addr_elem:
                    addr_text = await addr_elem.inner_text()
                    if addr_text and len(addr_text) > 5:
                        info["address"] = addr_text.strip()
                        break
            except:
                continue
        
        # Phone
        phone_selectors = [
            'button[data-item-id="phone"]',
            '[data-item-id="phone"]',
            'button[jsaction*="phone"]'
        ]
        
        for selector in phone_selectors:
            try:
                phone_elem = await page.query_selector(selector)
                if phone_elem:
                    phone_text = await phone_elem.inner_text()
                    # Clean phone number
                    phone_clean = re.sub(r'[^\d\(\)\-\+\s]', '', phone_text)
                    if phone_clean and len(phone_clean) >= 7:
                        info["phone"] = phone_clean.strip()
                        break
            except:
                continue
        
        # Website
        website_selectors = [
            'a[data-item-id="authority"]',
            'a[data-value="Website"]',
            '[data-item-id="authority"] a'
        ]
        
        for selector in website_selectors:
            try:
                web_elem = await page.query_selector(selector)
                if web_elem:
                    href = await web_elem.get_attribute("href")
                    if href and href.startswith('http'):
                        info["website"] = href
                        break
            except:
                continue
                
    except Exception as e:
        print_with_time(f"    Error extracting detailed info: {e}")
    
    return info

async def close_panel(page: Page):
    """Close the details panel"""
    try:
        close_selectors = [
            'button[aria-label="Back"]',
            'button[aria-label="Close"]',
            '[data-value="Back"]'
        ]
        
        for selector in close_selectors:
            try:
                await page.click(selector, timeout=3000)
                await human_delay(1, 2)
                return
            except:
                continue
        
        # Fallback: press Escape
        await page.keyboard.press("Escape")
        await human_delay(1, 2)
        
    except:
        pass

async def scroll_for_more_results(page: Page, results_selector: str, max_scrolls: int = 5):
    """Scroll to load more results"""
    print_with_time("Scrolling to load more results...")
    
    for scroll in range(max_scrolls):
        try:
            # Get current count
            current_count = await page.locator(results_selector).count()
            
            # Scroll the results container specifically
            try:
                # Find the scrollable results container
                feed_container = await page.query_selector('div[role="feed"]')
                if feed_container:
                    # Scroll within the feed container
                    await page.evaluate("""
                        (container) => {
                            container.scrollBy(0, container.scrollHeight);
                        }
                    """, feed_container)
                else:
                    # Fallback to window scroll
                    await page.evaluate("window.scrollBy(0, 1000)")
            except:
                await page.evaluate("window.scrollBy(0, 1000)")
            
            # Wait for new content to load
            await human_delay(3, 5)
            
            # Check if new results loaded
            new_count = await page.locator(results_selector).count()
            
            if new_count > current_count:
                print_with_time(f"  Scroll {scroll + 1}: {new_count - current_count} new results loaded")
            else:
                print_with_time(f"  Scroll {scroll + 1}: No new results, stopping")
                break
                
        except Exception as e:
            print_with_time(f"  Scroll error: {e}")
            break

async def scrape_all_hotels(page: Page, max_hotels: int = 30):
    """Main scraping function"""
    print_with_time("Starting hotel extraction...")
    
    # Wait for results and get the selector
    results_selector = await wait_for_results(page)
    
    # Scroll to load more results
    await scroll_for_more_results(page, results_selector)
    
    # Get total count
    total_results = await page.locator(results_selector).count()
    print_with_time(f"Found {total_results} total results to process")
    
    # Process results
    hotels = []
    processed_names = set()
    
    results_to_process = min(total_results, max_hotels)
    
    for i in range(results_to_process):
        try:
            print_with_time(f"Processing result {i + 1}/{results_to_process}")
            
            hotel_info = await extract_hotel_info_from_card(page, i, results_selector)
            
            if hotel_info and hotel_info["name"] not in processed_names:
                hotels.append(hotel_info)
                processed_names.add(hotel_info["name"])
                
                # Log extracted info
                rating_str = f" ({hotel_info['rating']}★)" if hotel_info.get('rating') else ""
                print_with_time(f"  ✓ Added: {hotel_info['name']}{rating_str}")
                
            elif hotel_info:
                print_with_time(f"  ⚠️ Duplicate: {hotel_info['name']}")
            
            # Random delay between extractions
            await human_delay(1, 3)
            
        except Exception as e:
            print_with_time(f"  ❌ Failed to process result {i + 1}: {e}")
            continue
    
    return hotels

def save_results(hotels: list[dict]):
    """Save results to CSV and JSON"""
    if not hotels:
        print_with_time("❌ No hotels to save")
        return
    
    print_with_time(f"Saving {len(hotels)} hotels...")
    
    # CSV
    fieldnames = ["name", "rating", "reviews", "price", "category", "address", "phone", "website"]
    with open(CSV_FILENAME, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for hotel in hotels:
            writer.writerow(hotel)
    
    # JSON
    with open(JSON_FILENAME, "w", encoding="utf-8") as jsonfile:
        json.dump(hotels, jsonfile, indent=2, ensure_ascii=False)
    
    print_with_time(f"✓ Saved to {CSV_FILENAME} and {JSON_FILENAME}")
    
    # Print summary
    print_with_time("\n" + "="*60)
    print_with_time(f"🏨 HOTELS FOUND: {len(hotels)}")
    print_with_time("="*60)
    
    for i, hotel in enumerate(hotels, 1):
        rating_str = f" ({hotel['rating']}★)" if hotel.get('rating') else ""
        reviews_str = f" - {hotel['reviews']} reviews" if hotel.get('reviews') else ""
        print_with_time(f"{i:2d}. {hotel['name']}{rating_str}{reviews_str}")
        
        if hotel.get('address'):
            print_with_time(f"     📍 {hotel['address']}")
        if hotel.get('phone'):
            print_with_time(f"     📞 {hotel['phone']}")
        if hotel.get('website'):
            print_with_time(f"     🌐 {hotel['website']}")
    
    print_with_time("="*60)

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Google Maps Hotels Scraper")
    parser.add_argument("--max-hotels", type=int, default=30, help="Maximum hotels to scrape")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--query", default=SEARCH_QUERY, help="Search query")
    args = parser.parse_args()
    
    print_with_time("🚀 Starting Google Maps Hotels Scraper...")
    
    browser = None
    try:
        async with async_playwright() as p:
            browser, page = await setup_browser(p, headless=args.headless)
            
            await navigate_and_search(page, args.query)
            hotels = await scrape_all_hotels(page, max_hotels=args.max_hotels)
            save_results(hotels)
            
    except Exception as e:
        print_with_time(f"❌ Script failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if browser:
            await browser.close()
            print_with_time("Browser closed.")

if __name__ == "__main__":
    asyncio.run(main())
