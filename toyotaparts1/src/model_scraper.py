import requests
import os
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging
import random
import time
from .partscategory import Category
from .parser import parse_model
from .data_handler import save_to_csv
from .part_scraper import ToyotaPartScraper
from .partslist_scraper import ToyotaPartsScraper
from .scraper import BaseScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ToyotaModelScraper(BaseScraper):
    def __init__(self, base_url: str = "https://www.toyotapartsdeal.com"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def fetch_page(self, url: str) -> Optional[str]:
        """Fetch the page content from the given URL."""
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.error(f"Error fetching {url}: {str(e)}")
            return None

    def scrape_model(self) -> Optional[List]:
        """Scrape list of all part numbers for given model."""
        url = f"{self.base_url}/2013-toyota-prius-plug_in-parts.html"
        html_content = self.fetch_page(url)

        if not html_content:
            return None

        # Save HTML to output/ for debugging
        os.makedirs("output", exist_ok=True)
        safe_filename = "category.html"
        output_path = os.path.join("output", safe_filename)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Saved HTML to {output_path}")

        soup = BeautifulSoup(html_content, 'html.parser')
        return parse_model(soup)


def main():
    # Example usage
    scraper = ToyotaModelScraper()
    
    # Scrape single part
    parts = scraper.scrape_model()
    if parts:
        #print(parts)
        for u in parts:
            #print(f"{part['number']}: {part['name']} -> {part['url']}")
            print(f"URL https://www.toyotapartsdeal.com{u}")
            scraper = ToyotaPartsScraper(f"https://www.toyotapartsdeal.com{u}")
    
            # Scrape single part
            partslist = scraper.scrape_partslist()
            if partslist:
                #save_to_csv([part_data], "toyota_partslist.csv")
                logger.info("Found partslist data count={}".format(len(partslist)))
                time.sleep(40)

                for part in partslist:
                    print(f"{part['number']}: {part['name']} -> {part['url']}")
                    pscraper = ToyotaPartScraper()
                    
                    # Scrape single part
                    part_data = pscraper.scrape_part_info(part['url'])
                    if part_data:
                        save_to_csv([part_data], "2013ToyotaPrius.csv")
                        logger.info("Data saved successfully")
                    else:
                        logger.error("Failed to scrape part data")

                    # Sleep for a random time between 1 and 35 seconds
                    delay = random.uniform(1, 35)
                    print(f"Sleeping for {delay:.2f} seconds...")
                    time.sleep(delay)
                #save_to_csv([part_data], "toyota_category.csv")
                logger.info("Got URL list successfully")
            else:
                logger.error("Failed to scrape part data")

if __name__ == "__main__":
    main() 