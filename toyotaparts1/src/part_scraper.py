import requests
import os
from bs4 import BeautifulSoup
from typing import Dict, Optional
import logging
from .parser import parse_part_details
from .data_handler import save_to_csv
from .scraper import BaseScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class ToyotaPartScraper(BaseScraper):
    def __init__(self, base_url: str = "https://www.toyotapartsdeal.com"):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def scrape_part_info(self, partpath: str = "/2013-toyota-prius-plug_in-parts.html") -> Optional[Dict]:
        """Scrape details about a part ."""
        url = f"{self.base_url}{partpath}"
        html_content = self.fetch_page(url)

        if not html_content:
            return None

        # Save HTML to output/ for debugging
        self.save_debug_html("partinfo", html_content)

        soup = BeautifulSoup(html_content, 'html.parser')
        return parse_part_details(soup)

def this_main():
    # Example usage
    scraper = ToyotaPartScraper()
    
    # Scrape single part
    part_data = scraper.scrape_part_info()
    if part_data:
        save_to_csv([part_data], "toyota_partslist.csv")
        logger.info("Data saved successfully")
    else:
        logger.error("Failed to scrape part data")

if __name__ == "__main__":
    this_main() 