"""Run Skagen Clothing's full men's clothing import."""

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

LOG = logging.getLogger("import_skagen_clothing_products")


def import_skagen_clothing_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.skagen_clothing import MEN_COLLECTION_HANDLE, SkagenClothingScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("SKAGEN_CLOTHING_PRODUCTS_TABLE", "skagen_clothing_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = SkagenClothingScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    try:
        summaries = (
            [{"handle": item, "_discovery_collections": []} for item in args.url]
            if args.url
            else scraper.discover_products(args.collection)
        )
        summaries = summaries[args.offset :]
        if args.limit:
            summaries = summaries[: args.limit]
        pending_handles = [
            scraper._handle_from_url(summary.get("handle") or summary.get("url"))
            for summary in summaries
            if summary.get("handle") or summary.get("url")
        ]
        queued_handles = set(pending_handles)
        discovery_by_handle = {
            scraper._handle_from_url(summary.get("handle") or summary.get("url")): summary
            for summary in summaries
            if summary.get("handle") or summary.get("url")
        }
        LOG.info("Selected %s Skagen Clothing men's clothing candidates.", len(pending_handles))
        if args.discover_only:
            for product in summaries[: args.preview]:
                LOG.info("Discovered Skagen Clothing product: %s (%s)", product.get("title"), product.get("handle"))
            return 0

        pending_rows: list[dict] = []
        dry_run_rows: list[dict] = []
        processed_handles: set[str] = set()
        while pending_handles:
            handle = pending_handles.pop(0)
            if not handle or handle in processed_handles:
                continue
            processed_handles.add(handle)
            if len(processed_handles) > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            LOG.info("[%s] Hydrating %s", len(processed_handles), handle)
            product = scraper.fetch_product(handle)
            if not scraper.is_clothing(product):
                LOG.info("Skipping non-clothing product: %s (%s)", product.get("title"), product.get("type"))
                continue
            summary = discovery_by_handle.get(handle) or {}
            product["_discovery_collections"] = summary.get("_discovery_collections") or []
            page_html = scraper.fetch_product_page(handle)
            row = scraper.product_to_row(product, page_html)
            for sibling_handle in scraper.color_handles(page_html):
                if sibling_handle not in queued_handles:
                    queued_handles.add(sibling_handle)
                    pending_handles.append(sibling_handle)
            if args.dry_run:
                dry_run_rows.append(row)
                continue
            pending_rows.append(row)
            if len(pending_rows) >= args.write_batch_size:
                assert client is not None
                client.upsert_products(table, pending_rows)
                LOG.info("Upserted %s Skagen Clothing rows to %s.", len(pending_rows), table)
                pending_rows.clear()

        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(dry_run_rows, ensure_ascii=False, indent=2))
        elif pending_rows:
            assert client is not None
            client.upsert_products(table, pending_rows)
            LOG.info("Upserted final %s Skagen Clothing rows to %s.", len(pending_rows), table)
        LOG.info("Skagen Clothing import complete. rows=%s", len(dry_run_rows) if args.dry_run else "written")
        return 0
    except Exception:
        LOG.exception("Skagen Clothing import failed")
        return 1


def parse_args() -> argparse.Namespace:
    from scrapers.full_import.skagen_clothing import MEN_COLLECTION_HANDLE

    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Full-import Skagen Clothing men's clothing and exact store stock.")
    parser.add_argument("--table", default=env("SKAGEN_CLOTHING_PRODUCTS_TABLE", "skagen_clothing_products"))
    parser.add_argument("--url", action="append", help="Import one product URL; linked colours are retained.")
    parser.add_argument("--collection", default=MEN_COLLECTION_HANDLE, help="Override the men's collection handle.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--write-batch-size", type=int, default=int(env("SKAGEN_CLOTHING_WRITE_BATCH_SIZE", "50")))
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    arguments = parser.parse_args()
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_skagen_clothing_products(arguments))
