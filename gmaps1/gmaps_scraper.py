import csv
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

def extract_business_info(business_element):
    try:
        # Initialize with default values
        business_info = {
            'name': 'UNKNOWN',
            'address': 'UNKNOWN',
            'rating': 'UNKNOWN',
            'reviews': 'UNKNOWN',
            'type': 'UNKNOWN',
            'phone': 'UNKNOWN',
            'website': 'UNKNOWN'
        }

        # Extract name - look for h3 with fontHeadlineSmall class
        name_elem = business_element.find('h3', {'class': 'fontHeadlineSmall'})
        if name_elem:
            business_info['name'] = name_elem.text.strip()

        # Extract address - look for div with address class
        address_elem = business_element.find('div', {'class': 'address'})
        if address_elem:
            business_info['address'] = address_elem.text.strip()

        # Extract rating and reviews - look for rating div
        rating_container = business_element.find('div', {'class': 'rating'})
        if rating_container:
            # Extract rating (e.g. "4.5")
            rating_span = rating_container.find('span', {'class': 'rating-score'})
            if rating_span:
                business_info['rating'] = rating_span.text.strip()

            # Extract review count (e.g. "123 reviews")
            reviews_span = rating_container.find('span', {'class': 'review-count'})
            if reviews_span:
                reviews_text = reviews_span.text.strip()
                # Clean up reviews text to just get the number
                reviews_num = ''.join(filter(str.isdigit, reviews_text))
                business_info['reviews'] = reviews_num if reviews_num else 'UNKNOWN'

        # Extract business type - look for type div
        type_elem = business_element.find('div', {'class': 'business-type'})
        if type_elem:
            business_info['type'] = type_elem.text.strip()

        # Extract phone number - look for phone div
        phone_elem = business_element.find('div', {'class': 'phone'})
        if phone_elem:
            business_info['phone'] = phone_elem.text.strip()

        # Extract website - look for website link
        website_elem = business_element.find('a', {'class': 'website'})
        if website_elem and 'href' in website_elem.attrs:
            business_info['website'] = website_elem['href']

        return business_info

    except Exception as e:
        print(f"Error extracting business info: {str(e)}")
        return business_info

def scrape_google_maps(search_query, num_results=20):
    """Scrape Google Maps for business information."""
    try:
        # Initialize Chrome with user data directory
        options = ChromeOptions()
        options.add_argument('--user-data-dir=/home/vijo/.config/google-chrome')
        options.add_argument('--profile-directory=Default')
        driver = webdriver.Chrome(options=options)
        
        # Navigate to Google Maps
        driver.get('https://www.google.com/maps')
        
        # Wait for search box and enter query
        search_box = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.ID, 'searchboxinput'))
        )
        search_box.send_keys(search_query)
        search_box.send_keys(Keys.RETURN)
        
        # Wait for results to load
        time.sleep(5)
        
        # Scroll to load more results
        last_height = driver.execute_script("return document.body.scrollHeight")
        while len(driver.find_elements(By.CLASS_NAME, 'Nv2PK')) < num_results:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
        
        # Get page source and parse with BeautifulSoup
        page_source = driver.page_source
        soup = BeautifulSoup(page_source, 'html.parser')
        
        # Find all business cards
        business_cards = soup.find_all('div', {'class': 'Nv2PK'})
        businesses = []
        
        # Extract information from each business card
        for card in business_cards:
            business_info = extract_business_info(card)
            if business_info:
                businesses.append(business_info)
        
        # Close the browser
        driver.quit()
        
        return businesses
        
    except Exception as e:
        print(f"Error during scraping: {str(e)}")
        if 'driver' in locals():
            driver.quit()
        return []

def main():
    # Example search query
    search_query = "coffee shops in Prunedale, CA"
    
    # Scrape Google Maps
    businesses = scrape_google_maps(search_query)
    
    if businesses:
        # Save results to CSV
        filename = f"coffee_shops_{search_query.split(' in ')[1].replace(', ', '_')}.csv"
        with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['name', 'address', 'rating', 'reviews', 'type', 'phone', 'website']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            writer.writeheader()
            for business in businesses:
                writer.writerow(business)
        
        print(f"Successfully scraped {len(businesses)} businesses and saved to {filename}")
    else:
        print("No businesses were scraped.")

if __name__ == "__main__":
    main() 