import scrapy


class GoogleMapsSpiderSpider(scrapy.Spider):
    name = "google_maps_spider"
    allowed_domains = ["maps.google.com"]
    start_urls = ["https://maps.google.com"]

    def parse(self, response):
        self.log(f'Got response from {response.url}')
