"""Run SuitClub's full import of men's clothing and footwear."""

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

LOG = logging.getLogger("import_suitclub_products")


def import_suitclub_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.suitclub import DEFAULT_COLLECTION_HANDLES, SuitClubScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("SUITCLUB_PRODUCTS_TABLE", "suitclub_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = SuitClubScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    try:
        if args.url:
            summaries = []
            for url in args.url:
                product = scraper.fetch_product(url)
                product["_discovery_collections"] = []
                if scraper.is_catalog_product(product):
                    summaries.append(product)
                else:
                    LOG.warning("Skipping out-of-scope SuitClub product: %s (%s)", product.get("title"), product.get("type"))
        else:
            summaries = scraper.discover_products(args.collection or DEFAULT_COLLECTION_HANDLES)

        summaries = summaries[args.offset :]
        if args.limit:
            summaries = summaries[: args.limit]
        LOG.info("Selected %s SuitClub clothing and footwear products.", len(summaries))

        if args.discover_only:
            for product in summaries[: args.preview]:
                LOG.info(
                    "Discovered SuitClub product: %s (%s) type=%s variants=%s",
                    product.get("title"),
                    product.get("handle"),
                    product.get("type") or product.get("product_type"),
                    len(product.get("variants") or []),
                )
            return 0
        if not summaries:
            LOG.info("No SuitClub products selected.")
            return 0

        first_handle = summaries[0].get("handle") or summaries[0].get("url")
        first_page_html = scraper.fetch_product_page(str(first_handle))
        page_cache = {scraper._handle_from_url(str(first_handle)): first_page_html}
        inventory_snapshot = scraper.fetch_inventory_snapshot(
            [product["id"] for product in summaries],
            first_page_html,
            batch_size=args.inventory_batch_size,
            batch_delay=0 if args.no_delay else args.inventory_batch_delay,
        )
        LOG.info(
            "Fetched exact SuitClub inventory for %s products and %s locations.",
            len(inventory_snapshot["products"]),
            len(inventory_snapshot["locations"]),
        )

        pending_rows: list[dict] = []
        dry_run_rows: list[dict] = []
        for index, product in enumerate(summaries, start=1):
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            handle = scraper._handle_from_url(str(product.get("handle") or product.get("url")))
            LOG.info("[%s/%s] Hydrating %s", index, len(summaries), handle)
            page_html = page_cache.pop(handle, None) or scraper.fetch_product_page(handle)
            row = scraper.product_to_row(product, page_html, inventory_snapshot)

            if args.dry_run:
                dry_run_rows.append(row)
                continue
            pending_rows.append(row)
            if len(pending_rows) >= args.write_batch_size:
                assert client is not None
                client.upsert_products(table, pending_rows, on_conflict="source_product_id")
                LOG.info("Upserted %s SuitClub rows to %s.", len(pending_rows), table)
                pending_rows.clear()

        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(dry_run_rows, ensure_ascii=False, indent=2))
        elif pending_rows:
            assert client is not None
            client.upsert_products(table, pending_rows, on_conflict="source_product_id")
            LOG.info("Upserted final %s SuitClub rows to %s.", len(pending_rows), table)
        LOG.info("SuitClub import complete. rows=%s", len(dry_run_rows) if args.dry_run else len(summaries))
        return 0
    except Exception:
        LOG.exception("SuitClub import failed")
        return 1


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Full-import SuitClub men's clothing and footwear with exact store stock."
    )
    parser.add_argument("--table", default=env("SUITCLUB_PRODUCTS_TABLE", "suitclub_products"))
    parser.add_argument("--url", action="append", help="Import one in-scope SuitClub product URL.")
    parser.add_argument(
        "--collection",
        action="append",
        help="Override discovery with one collection handle; repeat to combine collections.",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--write-batch-size", type=int, default=int(env("SUITCLUB_WRITE_BATCH_SIZE", "50")))
    parser.add_argument(
        "--inventory-batch-size",
        type=int,
        default=int(env("SUITCLUB_INVENTORY_BATCH_SIZE", "10")),
        help="Number of Shopify products per Storefront inventory query.",
    )
    parser.add_argument(
        "--inventory-batch-delay",
        type=float,
        default=float(env("SUITCLUB_INVENTORY_BATCH_DELAY", "0.5")),
        help="Delay between Storefront inventory batches.",
    )
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    arguments = parser.parse_args()
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    if arguments.inventory_batch_size < 1:
        parser.error("--inventory-batch-size must be at least 1")
    if arguments.inventory_batch_delay < 0:
        parser.error("--inventory-batch-delay cannot be negative")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_suitclub_products(arguments))
