import subprocess
import sys
import re
import traceback
import csv
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import os
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from bs4 import BeautifulSoup
import logging
import random
from fake_useragent import UserAgent

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_version(cmd):
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return result.stdout.strip()
    except Exception:
        return ""

def get_major_version(version_str):
    match = re.search(r"(\d+)\.", version_str)
    return int(match.group(1)) if match else None

def check_chrome_and_driver():
    chrome_version = get_version(["google-chrome", "--version"]) or get_version(["/usr/bin/google-chrome", "--version"])
    chromedriver_version = get_version(["chromedriver", "--version"])

    if not chrome_version:
        print("❌ Google Chrome not found.")
        sys.exit(1)

    if not chromedriver_version:
        print("❌ ChromeDriver not found.")
        sys.exit(1)

    chrome_major = get_major_version(chrome_version)
    driver_major = get_major_version(chromedriver_version)

    print(f"✅ Chrome: {chrome_version}")
    print(f"✅ ChromeDriver: {chromedriver_version}")

    if chrome_major != driver_major:
        print(f"❌ Version mismatch: Chrome {chrome_major} vs ChromeDriver {driver_major}")
        print("➡️  Please install the matching ChromeDriver version.")
        sys.exit(1)

def get_random_user_agent():
    ua = UserAgent()
    return ua.random

def search_google_maps(query, location, num_results=20, max_retries=3):
    driver = None
    for attempt in range(max_retries):
        try:
            # Set up Chrome options
            chrome_options = Options()
            chrome_options.add_argument('--headless=new')
            chrome_options.add_argument('--no-sandbox')
            chrome_options.add_argument('--disable-dev-shm-usage')
            chrome_options.add_argument('--disable-gpu')
            chrome_options.add_argument('--window-size=1920,1080')
            chrome_options.add_argument('--disable-extensions')
            chrome_options.add_argument('--disable-software-rasterizer')
            chrome_options.add_argument('--disable-blink-features=AutomationControlled')
            chrome_options.add_argument('--disable-features=IsolateOrigins,site-per-process')
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            
            # Add random user agent
            user_agent = get_random_user_agent()
            chrome_options.add_argument(f'user-agent={user_agent}')
            
            # Create a unique user data directory
            user_data_dir = f"/tmp/chrome_{os.getpid()}_{int(time.time())}"
            os.makedirs(user_data_dir, exist_ok=True)
            chrome_options.add_argument(f'--user-data-dir={user_data_dir}')
            
            logger.info(f"Starting Chrome with user data dir: {user_data_dir}")
            
            # Initialize the Chrome WebDriver with options
            service = Service()
            driver = webdriver.Chrome(service=service, options=chrome_options)
            
            # Set page load timeout
            driver.set_page_load_timeout(30)
            
            # Execute CDP commands to prevent detection
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => undefined
                    })
                '''
            })
            
            logger.info("Chrome WebDriver initialized successfully")
            
            # Construct the search URL
            search_url = f"https://www.google.com/maps/search/{query}+in+{location}"
            logger.info(f"Attempt {attempt + 1}/{max_retries}: Navigating to: {search_url}")
            driver.get(search_url)
            
            # Add random delay before interacting with the page
            time.sleep(random.uniform(2, 5))
            
            # Wait for the results to load with multiple possible selectors
            wait = WebDriverWait(driver, 20)
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='feed']")))
            except TimeoutException:
                # Try alternative selector
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div.Nv2PK")))
            
            logger.info("Results page loaded successfully")
            
            # Scroll to load more results with random delays
            last_height = driver.execute_script("return document.body.scrollHeight")
            scroll_count = 0
            while True:
                # Add random delay between scrolls
                time.sleep(random.uniform(1, 3))
                
                # Scroll down
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                
                # Calculate new scroll height
                new_height = driver.execute_script("return document.body.scrollHeight")
                if new_height == last_height:
                    break
                last_height = new_height
                scroll_count += 1
                
                # Check if we have enough results
                results = driver.find_elements(By.CSS_SELECTOR, "div[role='feed'] > div")
                logger.info(f"Found {len(results)} results after {scroll_count} scrolls")
                if len(results) >= num_results:
                    break
                
                # Add longer delay every few scrolls
                if scroll_count % 3 == 0:
                    time.sleep(random.uniform(3, 6))
            
            # Get the page source and parse with BeautifulSoup
            page_source = driver.page_source
            soup = BeautifulSoup(page_source, 'html.parser')
            
            # Save debug page source
            with open('debug_page_source.html', 'w', encoding='utf-8') as f:
                f.write(page_source)
            logger.info("Saved debug page source")
            
            # Extract business information
            businesses = []
            # Look for business cards with multiple possible selectors
            results = soup.select("div[role='feed'] > div")
            if not results:
                results = soup.select("div.Nv2PK")
            if not results:
                results = soup.select("div[role='article']")
                
            logger.info(f"Found {len(results)} results to process")
            
            for i, result in enumerate(results[:num_results]):
                try:
                    # Extract basic info from the card
                    business_data = extract_business_info(result)
                    
                    # Only process businesses that have at least a name
                    if business_data['name'] != "UNKNOWN":
                        # Try to get phone and website by clicking the card
                        try:
                            # Find the business card by name using multiple selectors
                            business_card = None
                            selectors = [
                                f"//div[contains(@class, 'qBF1Pd') and contains(text(), '{business_data['name']}')]/ancestor::div[contains(@class, 'Nv2PK')]",
                                f"//div[contains(@class, 'qBF1Pd') and contains(text(), '{business_data['name']}')]/ancestor::div[contains(@role, 'article')]",
                                f"//div[contains(@class, 'fontHeadlineSmall') and contains(text(), '{business_data['name']}')]/ancestor::div[contains(@class, 'Nv2PK')]"
                            ]
                            
                            for selector in selectors:
                                try:
                                    business_card = wait.until(EC.presence_of_element_located((By.XPATH, selector)))
                                    if business_card:
                                        break
                                except:
                                    continue
                            
                            if business_card:
                                # Scroll the card into view
                                driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", business_card)
                                time.sleep(random.uniform(0.5, 1))
                                
                                # Click on the card using JavaScript
                                driver.execute_script("arguments[0].click();", business_card)
                                time.sleep(random.uniform(1, 2))  # Wait for the expanded view to load
                                
                                # Extract phone and website from expanded view
                                expanded_source = driver.page_source
                                expanded_soup = BeautifulSoup(expanded_source, 'html.parser')
                                
                                # Look for phone number
                                phone_element = expanded_soup.find('button', {'data-tooltip': 'Copy phone number'})
                                if phone_element:
                                    business_data['phone'] = phone_element.text.strip()
                                
                                # Look for website
                                website_element = expanded_soup.find('a', {'data-tooltip': 'Open website'})
                                if website_element and 'href' in website_element.attrs:
                                    business_data['website'] = website_element['href']
                                
                                # Close the expanded view using JavaScript
                                try:
                                    close_button = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "button[aria-label='Close']")))
                                    driver.execute_script("arguments[0].click();", close_button)
                                    time.sleep(random.uniform(0.5, 1))
                                except:
                                    logger.warning(f"Could not close expanded view for {business_data['name']}")
                            
                        except Exception as e:
                            logger.warning(f"Could not expand business card for {business_data['name']}: {str(e)}")
                            # Set phone and website to UNKNOWN if we couldn't get them
                            business_data['phone'] = "UNKNOWN"
                            business_data['website'] = "UNKNOWN"
                        
                        logger.info(f"Extracted business: {business_data['name']}")
                        businesses.append(business_data)
                    
                except Exception as e:
                    logger.error(f"Error extracting business info: {str(e)}")
                    continue
            
            # Save results to CSV
            save_to_csv(businesses, query, location)
            logger.info(f"Saved {len(businesses)} businesses to CSV")
            
            # Take a screenshot
            driver.save_screenshot(f"{query}_{location}.png")
            logger.info("Saved screenshot")
            
            return businesses
            
        except Exception as e:
            logger.error(f"Error during search (attempt {attempt + 1}/{max_retries}): {str(e)}")
            if attempt < max_retries - 1:
                logger.info(f"Retrying in {5 * (attempt + 1)} seconds...")
                time.sleep(5 * (attempt + 1))
            else:
                logger.error("Max retries reached. Giving up.")
                return []
            
        finally:
            if driver:
                try:
                    driver.quit()
                    logger.info("Chrome WebDriver closed successfully")
                except:
                    pass

def extract_business_info(element):
    try:
        # Initialize with default values
        business_info = {
            'name': "UNKNOWN",
            'address': "UNKNOWN",
            'rating': "UNKNOWN",
            'reviews': "UNKNOWN",
            'type': "UNKNOWN",
            'phone': "UNKNOWN",
            'website': "UNKNOWN",
            'maps_url': "UNKNOWN"
        }
        
        # Extract business name
        name_element = element.find('div', {'class': 'qBF1Pd fontHeadlineSmall'})
        if name_element:
            business_info['name'] = name_element.text.strip()
            # Construct Google Maps URL
            business_info['maps_url'] = f"https://www.google.com/maps/search/{business_info['name'].replace(' ', '+')}"
        
        # Extract rating and reviews from aria-label
        rating_container = element.find('span', {'class': 'ZkP5Je'})
        if rating_container and 'aria-label' in rating_container.attrs:
            rating_text = rating_container['aria-label']
            # Extract rating and reviews (e.g. "4.2 stars 556 Reviews")
            rating_match = re.search(r'(\d+\.?\d*) stars (\d+) Reviews', rating_text)
            if rating_match:
                business_info['rating'] = rating_match.group(1)
                business_info['reviews'] = rating_match.group(2)
        
        # Extract business type and address
        type_address_elements = element.find_all('div', {'class': 'W4Efsd'})
        for elem in type_address_elements:
            # Look for business type
            type_span = elem.find('span', string=lambda text: text and text.strip() in ['Coffee shop', 'Bakery', 'Donuts'])
            if type_span:
                business_info['type'] = type_span.text.strip()
            
            # Look for address
            address_span = elem.find('span', string=lambda text: text and any(road in text for road in ['Rd', 'St', 'Ave', 'Blvd']))
            if address_span:
                business_info['address'] = address_span.text.strip()
        
        # Note: Phone numbers and websites are not directly visible in the initial HTML
        # They require clicking on the business card to expand it
        # We'll need to implement a separate function to handle this if needed
        
        logger.info(f"Successfully extracted business info: {business_info['name']}")
        return business_info
        
    except Exception as e:
        logger.error(f"Error extracting business info: {str(e)}")
        logger.error(f"Element HTML: {element.prettify()}")
        return business_info

def save_to_csv(businesses, query, location):
    filename = f"{query}_{location}.csv"
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['name', 'address', 'rating', 'reviews', 'type', 'phone', 'website', 'maps_url']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for business in businesses:
            writer.writerow(business)

if __name__ == "__main__":
    check_chrome_and_driver()


    # Use CLI arg as query if passed, otherwise default to "coffee shops"
    query = sys.argv[1] if len(sys.argv) > 1 else "coffee shops"
    location = "Prunedale,CA"

    results = search_google_maps(query, location)
    print(f"Found {len(results)} businesses")
