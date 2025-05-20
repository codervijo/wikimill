import requests
import os
from bs4 import BeautifulSoup
from typing import List, Dict, Optional
import logging
import random
import time
from .parser import parse_dealz
from .data_handler import save_to_csv
from .scraper import BaseScraper

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class GlitchnDealzScraper(BaseScraper):
    def __init__(self, base_url: str = "https://glitchndealz.com/"):
        print("Initializing GlitchnDealzScraper")
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/',
            'DNT': '1',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
        print("Session headers set:")
        for k, v in self.session.headers.items():
            print(f"  {k}: {v}")

    def fetch_page(self, url: str) -> Optional[str]:
        """Fetch the page content from the given URL."""
        print(f"\nFetching URL: {url}")
        try:
            response = self.session.get(url)
            print(f"Status Code: {response.status_code}")
            response.raise_for_status()
            print(f"Successfully fetched {url} (content length = {len(response.text)} characters)")
            return response.text
        except requests.RequestException as e:
            print(f"Error fetching {url}: {str(e)}")
            return None

    def scrape_dealz(self, pageno=1) -> Optional[List]:
        """Scrape list of all part numbers for given model."""
        url = f"{self.base_url}page/{pageno}"
        html_content = self.fetch_page(url)

        if not html_content:
            return None

        # Save HTML to output/ for debugging
        self.save_debug_html(f"glitchndealz_page_{pageno}", html_content)

        soup = BeautifulSoup(html_content, 'html.parser')
        return parse_dealz(soup)

def main():
    dealz = GlitchnDealzScraper()
    for p in range(1, 200):
        deals = dealz.scrape_dealz(p)
        if deals:
            print("\n=== Deals ===")
            print(f"{'No.':<4} {'Deal'}")
            print("-" * 40)
            for i, deal_name in enumerate(deals, 1):
                print(f"{i:<4} {deal_name}")
        save_to_csv(deals, "dealz.csv")
    else:
        print("No deals found.")

if __name__ == "__main__":
    main() 