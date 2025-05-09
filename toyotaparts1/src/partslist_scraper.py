import requests
import os
from bs4 import BeautifulSoup
from typing import Dict, Optional
import logging
import time
import random
from .parser import parse_partslist
from .data_handler import save_to_csv
from .part_scraper import ToyotaPartScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ToyotaPartsScraper:
    def __init__(self, base_url: str = "https://www.toyotapartsdeal.com/parts-list/2013-toyota-prius-plug_in/body/armrest_visor.html"):
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

    def scrape_partslist(self) -> Optional[Dict]:
        """Scrape list of all part numbers for given model."""
        url = f"{self.base_url}"
        html_content = self.fetch_page(url)

        if not html_content:
            return None

        # Save HTML to output/ for debugging
        os.makedirs("output", exist_ok=True)
        safe_filename = "partslist.html"
        output_path = os.path.join("output", safe_filename)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Saved HTML to {output_path}")

        soup = BeautifulSoup(html_content, 'html.parser')
        return parse_partslist(soup)

def main():
    # Example usage
    scraper = ToyotaPartsScraper()
    
    # Scrape single part
    partslist = scraper.scrape_partslist()
    if partslist:
        #save_to_csv([part_data], "toyota_partslist.csv")
        logger.info("Data saved successfully")

        for part in partslist:
            print(f"{part['number']}: {part['name']} -> {part['url']}")
            pscraper = ToyotaPartScraper()
            
            # Scrape single part
            part_data = pscraper.scrape_part_info(part['url'])
            if part_data:
                save_to_csv([part_data], "toyota_partslist.csv")
                logger.info("Data saved successfully")
            else:
                logger.error("Failed to scrape part data")

            # Sleep for a random time between 1 and 35 seconds
            delay = random.uniform(1, 35)
            print(f"Sleeping for {delay:.2f} seconds...")
            time.sleep(delay)

    else:
        logger.error("Failed to scrape part data")

if __name__ == "__main__":
    main() 