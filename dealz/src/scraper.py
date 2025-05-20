import os
import logging
import requests
from bs4 import BeautifulSoup
from typing import Optional, List, Dict, Callable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class BaseScraper:
    def __init__(
        self,
        base_url: str,
        parser_fn: Callable[[BeautifulSoup], Dict],
    ):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.parser_fn = parser_fn

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def fetch_page(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.text
        except requests.RequestException as e:
            logger.error(f"[{self.brand_name}] Error fetching {url}: {str(e)}")
            return None

    # Save HTML to output/ for debugging
    def save_debug_html(self, filename: str, html: str):
        os.makedirs("output", exist_ok=True)
        filename = filename.replace("~", "_") + ".html"
        path = os.path.join("output", filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Debug HTML Saved HTML to {path}")
