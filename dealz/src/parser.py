from bs4 import BeautifulSoup
from typing import Dict, List
import logging

logger = logging.getLogger(__name__)

def parse_part_details(soup: BeautifulSoup) -> Dict:
    """Parse the part details from the HTML content."""
    try:
        # Initialize the result dictionary with specific fields
        part_data = {
            'brand': 'Toyota',  # Default brand
            'part_number': '',
            'dimensions': '',
            'weight': '',
            'name': '',
            'price': '',
            'description': '',
            'compatibility': [],
            'specifications': {}
        }
        autopart = Part()

        #autopart.set_url()
        # Extract part number from the title
        title_element = soup.find('h1')
        if title_element:
            title_text = title_element.text.strip()
            part_data['name'] = title_text
            # Try to extract part number from title
            if 'Toyota' in title_text:
                part_number = title_text.split('Toyota')[-1].strip()
                part_data['part_number'] = part_number
                autopart.set_part_number(part_number)

        # Extract price
        price_element = soup.find('span', class_='price')
        if price_element:
            part_data['price'] = price_element.text.strip()

        # Extract current price
        price = soup.select_one('.price-section-price')
        price_text = price.get_text(strip=True).replace('$', '') if price else None
        autopart.set_price(price_text)

        # Extract MSRP
        msrp = soup.select_one('.price-section-retail span')
        msrp_text = msrp.get_text(strip=True).replace('$', '') if msrp else None
        autopart.set_msrp(msrp_text)

        # Extract savings
        savings = soup.select_one('.price-section-save')
        savings_text = savings.get_text(strip=True).replace('You Save: $', '') if savings else None

        # Optionally split savings and percentage
        savings_amount, savings_percent = None, None
        if savings_text:
            parts = savings_text.replace(')', '').split('(')
            savings_amount = parts[0].strip()
            if len(parts) > 1:
                savings_percent = parts[1].strip()
            autopart.set_savings(savings_amount)

        # Extract specifications from the product specifications table
        specs_table = soup.find('table', class_='pn-spec-list')
        if specs_table:
            rows = specs_table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                if len(cells) == 2:
                    key = cells[0].text.strip().lower()
                    value = cells[1].text.strip()
                    #print(key, value)

                    # Map specific fields
                    if 'part number' in key:
                        part_data['part_number'] = value
                        autopart.set_part_number(value)
                    elif 'dimensions' in key or 'size' in key:
                        part_data['dimensions'] = value
                        autopart.set_dimensions(value)
                    elif 'weight' in key:
                        part_data['weight'] = value
                        autopart.set_weight(value)
                    elif 'brand' in key:
                        autopart.set_brand(value)
                    elif 'manufacturer' in key:
                        autopart.set_manufacturer(value)
                    elif 'condition' in key:
                        autopart.set_condition(value)
                    elif 'description' in key:
                        autopart.set_description(value)
                    elif 'position' in key:
                        autopart.set_position(value)
                    elif 'other names' in key:
                        autopart.set_other_names(value)
                    elif 'warranty' in key:
                        autopart.set_warranty(value)

                    # Store all specifications
                    part_data['specifications'][key] = value

        base_url="https://tpd.com/"
        # Find all <img> tags within carousel items
        img_tags = soup.select('.img-carousel-img img')
        # Construct full URLs for all images
        image_urls = [base_url + img['src'] for img in img_tags if img.get('src')]

        # Print result
        print("Image URLs:")
        for url in image_urls:
            print(url)

        print(autopart)

        return part_data

    except Exception as e:
        logger.error(f"Error parsing part details: {str(e)}")
        return {}

def parse_partslist(soup: BeautifulSoup) -> Dict:
    """Parse the parts list from the HTML content."""
    try:
        all_parts = []

        for part_block in soup.select('div.pl-pat-im-sub'):
            a_tags = part_block.select('a.pl-pat-im-link')
            if len(a_tags) >= 2:
                part_number = a_tags[0].text.strip()
                part_name = a_tags[1].text.strip()
                part_url = a_tags[1]['href']
                all_parts.append({
                    'name': part_name,
                    'number': part_number,
                    'url': part_url
                })

        # Example: print all part info
        for part in all_parts:
            print(f"{part['number']}: {part['name']} -> {part['url']}")

        return all_parts

    except Exception as e:
        logger.error(f"Error parsing part details: {str(e)}")
        return {}

class Dealz:
    def __init__(self):
        self.resources = []

    def add_resource(self, name: str, url: str):
        self.resources.append((name, url))

    def get_part_info(self) -> List[tuple]:
        return self.resources

    def __str__(self):
        return f"Dealz with {len(self.resources)} items"

def parse_dealz(soup: BeautifulSoup) -> Dict:
    """Parse the dealz list from the HTML content."""
    try:
        dealz = Dealz()

        # Select all news blocks
        news_items = soup.select('div.newsdetail.newstitleblock.rh_gr_right_sec')

        for item in news_items:
            a_tag = item.find("a")
            if a_tag and a_tag.has_attr("href"):
                name = a_tag.get_text(strip=True)
                url = a_tag["href"].strip()
                dealz.add_resource(name, url)
        #print("Dealz" + dealz)
        for name, url in dealz.get_part_info():
            print(f"{name} -> {url}")

        #all_names = [name for name,_ in dealz.get_part_info()]
        #return all_names
        return dealz.get_part_info()

    except Exception as e:
        logger.error(f"Error parsing model parts: {str(e)}")
        return {} 