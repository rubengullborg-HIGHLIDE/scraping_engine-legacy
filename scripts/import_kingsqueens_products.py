"""Full import of Kings & Queens men's clothing and shoes.

One pickup-availability request per size gives yes/no availability for the
Bruun's Galleri shop in Aarhus. See scrapers/full_import/kings_queens.py.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scrapers.full_import.kings_queens import KingsQueensScraper
from scripts.shopify_import import ShopifyStore, main

STORE = ShopifyStore(
    key="kingsqueens",
    label="Kings & Queens",
    default_table="kingsqueens_products",
    make_scraper=KingsQueensScraper,
    hydrate=lambda scraper, product, args: scraper.product_to_row(
        product,
        scraper.fetch_aarhus_availability(product, variant_delay=0 if args.no_delay else args.variant_delay),
    ),
    example_url="https://kingsqueens.dk/products/dk0a8cckblk1-black",
)

if __name__ == "__main__":
    raise SystemExit(main(STORE))
