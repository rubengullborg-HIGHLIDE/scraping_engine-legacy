"""Run CEJF's full import of the men's Shopify collection."""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.import_rains_products import SupabaseCatalogClient, env, load_dotenv

LOG = logging.getLogger("import_cejf_products")


def import_cejf_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.cejf import MEN_COLLECTION_HANDLE, CejfScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("CEJF_PRODUCTS_TABLE", "cejf_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = CejfScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    try:
        if args.url:
            products = []
            for index, url in enumerate(args.url):
                if index and not args.no_delay:
                    time.sleep(random.uniform(args.min_delay, args.max_delay))
                product = scraper.fetch_product(url)
                product["_discovery_collections"] = []
                if scraper.is_mens_product(product):
                    products.append(product)
                else:
                    LOG.warning("Skipping CEJF product without the men tag: %s", product.get("title"))
        else:
            products = scraper.discover_products(args.collection or MEN_COLLECTION_HANDLE)

        products = products[args.offset :]
        if args.limit:
            products = products[: args.limit]
        LOG.info("Selected %s CEJF men's products.", len(products))

        if args.discover_only:
            for product in products[: args.preview]:
                LOG.info(
                    "Discovered CEJF product: %s (%s) variants=%s",
                    product.get("title"),
                    product.get("handle"),
                    len(product.get("variants") or []),
                )
            return 0
        if not products:
            LOG.info("No CEJF products selected.")
            return 0

        rows = [scraper.product_to_row(product) for product in products]
        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(rows, ensure_ascii=False, indent=2))
            return 0

        assert client is not None
        for offset in range(0, len(rows), args.write_batch_size):
            batch = rows[offset : offset + args.write_batch_size]
            client.upsert_products(table, batch, on_conflict="source_product_id")
            LOG.info("Upserted %s CEJF rows to %s.", len(batch), table)
        LOG.info("CEJF import complete. rows=%s", len(rows))
        return 0
    except Exception:
        LOG.exception("CEJF import failed")
        return 1


def parse_args() -> argparse.Namespace:
    from scrapers.full_import.cejf import MEN_COLLECTION_HANDLE

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Full-import CEJF men's clothing with published webshop availability."
    )
    parser.add_argument("--table", default=env("CEJF_PRODUCTS_TABLE", "cejf_products"))
    parser.add_argument("--url", action="append", help="Import one CEJF men's product URL.")
    parser.add_argument("--collection", default=MEN_COLLECTION_HANDLE, help="Override the men's collection handle.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--write-batch-size", type=int, default=int(env("CEJF_WRITE_BATCH_SIZE", "50")))
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    arguments = parser.parse_args()
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_cejf_products(arguments))
