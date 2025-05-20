# Dealz Scraper

A modular web scraper for extracting Toyota parts information from ToyotaPartsDeal.com.

## Features

- Scrapes part details including name, price, description, compatibility, and specifications
- Saves data to CSV format
- Modular design for easy extension
- Error handling and logging
- Session management for efficient scraping

## Installation

1. Clone the repository
2. Create a virtual environment (recommended):
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. Navigate to the project directory:
   ```bash
   cd toyotaparts1
   ```

2. Run the scraper:
   ```bash
   python -m src.scraper
   ```

3. To scrape specific parts, modify the `main()` function in `src/scraper.py`:
   ```python
   scraper = ToyotaPartsScraper()
   part_numbers = ["part1", "part2", "part3"]
   results = scraper.scrape_parts(part_numbers)
   save_to_csv(results, "toyota_parts.csv")
   ```

## Project Structure

```
dealz/
├── output/            # Directory for output CSV files
├── src/
│   ├── scraper.py     # Main scraper class
│   ├── parser.py      # HTML parsing functions
│   └── data_handler.py # CSV handling functions
├── requirements.txt    # Project dependencies
└── README.md          # Project documentation
```

## Extending the Scraper

1. To add new parsing functionality, modify the `parse_part_details()` function in `parser.py`
2. To change the output format, modify the `save_to_csv()` function in `data_handler.py`
3. To add new scraping features, extend the `ToyotaPartsScraper` class in `scraper.py`

## License

MIT License 