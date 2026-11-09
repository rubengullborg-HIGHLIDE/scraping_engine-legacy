"""Full import of Takeoff Copenhagen sneakers, streetwear and preloved.

One inventory request per product gives exact stock for Takeoff Aarhus and
Takeoff Copenhagen. See scrapers/full_import/takeoff.py.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapers.full_import.takeoff import TakeoffScraper
from scripts.shopify_import import ShopifyStore, main

STORE = ShopifyStore(
    key="takeoff",
    label="Takeoff Copenhagen",
    default_table="takeoff_products",
    make_scraper=TakeoffScraper,
    hydrate=lambda scraper, product, args: scraper.product_to_row(product, scraper.fetch_inventory(product["id"])),
    example_url="https://takeoffcph.dk/products/air-max-1-white-pure-platinum-black-leather",
)

if __name__ == "__main__":
    raise SystemExit(main(STORE))
