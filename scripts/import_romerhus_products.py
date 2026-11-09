from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.import_outcomes import ItemOutcomes
from scripts.import_rains_products import SupabaseCatalogClient
from scripts.catalog_manifest import manifest_items, add_manifest_argument

LOG = logging.getLogger("import_romerhus_products")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return default if value is None or value == "" else value


def import_romerhus_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.romerhus import MEN_COLLECTION_HANDLES, RomerhusScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("ROMERHUS_PRODUCTS_TABLE", "romerhus_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = RomerhusScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    outcomes = ItemOutcomes("romerhus", LOG)
    try:
        products = [scraper.fetch_product(url) for url in args.url] if args.url else manifest_items(args, "romerhus", lambda: scraper.discover_products(args.collection or MEN_COLLECTION_HANDLES))
        products = products[args.offset :]
        if args.limit:
            products = products[: args.limit]
        LOG.info("Discovered %s unique Rømerhus men's colour products.", len(products))
        if args.discover_only:
            for product in products[: args.preview]:
                LOG.info("Discovered product: %s (%s)", product.get("title"), product.get("handle"))
            return 0
        if not args.url:
            summaries = products
            products = []
            for index, summary in enumerate(summaries, start=1):
                if index > 1 and not args.no_delay:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
                LOG.info("[%s/%s] Hydrating %s", index, len(summaries), summary.get("handle"))
                with outcomes.item(str(summary.get("handle"))):
                    products.append(scraper.fetch_product(summary["handle"]))
        elif products and not args.no_delay:
            time.sleep(random.uniform(args.min_delay, args.max_delay))
        rows = []
        if products:
            # Store locations and stock are fetched once per batch; failures
            # there are store-wide and fail the batch.
            locations = {int(location["id"]): location for location in scraper.fetch_store_locations() if location.get("id") is not None}
            stores = scraper._tracked_stores(locations)
            stock_by_variant = scraper.fetch_variant_stock(
                int(variant["id"]) for product in products for variant in product.get("variants", []) if variant.get("id") is not None
            )
            for product in products:
                with outcomes.item(str(product.get("handle") or product.get("id"))):
                    rows.append(scraper.product_to_row(product, stock_by_variant, stores))
        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(rows, ensure_ascii=False, indent=2))
        elif rows:
            assert client is not None
            client.upsert_products(table, rows)
        LOG.info("Rømerhus import complete. rows=%s", len(rows))
        return outcomes.exit_code()
    except Exception:
        LOG.exception("Rømerhus import failed")
        return 1


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Full-import Rømerhus men's products and exact local store stock.")
    add_manifest_argument(parser)
    parser.add_argument("--table", default=env("ROMERHUS_PRODUCTS_TABLE", "romerhus_products"))
    parser.add_argument("--url", action="append", help="Import one product URL.")
    parser.add_argument("--collection", action="append", help="Limit discovery to a Rømerhus collection handle.")
    parser.add_argument("--limit", type=int, help="Limit discovered product colours for testing.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many discovered product colours.")
    parser.add_argument("--discover-only", action="store_true", help="Only list discovered product colours.")
    parser.add_argument("--preview", type=int, default=10, help="Number of discovered products to log.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape without writing to Supabase.")
    parser.add_argument("--no-delay", action="store_true", help="Disable the short polite delay before stock requests.")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_romerhus_products(arguments))
