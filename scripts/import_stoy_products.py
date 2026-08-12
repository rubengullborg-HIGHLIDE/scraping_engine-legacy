"""Run STOY's full men's clothing import."""

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

LOG = logging.getLogger("import_stoy_products")


def import_stoy_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.stoy import MEN_COLLECTION_HANDLES, StoyScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("STOY_PRODUCTS_TABLE", "stoy_products")
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not url or not key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")
    scraper = StoyScraper()
    client = SupabaseCatalogClient(url, key) if not args.dry_run else None
    try:
        summaries = [{"handle": item} for item in args.url] if args.url else scraper.discover_products(args.collection or MEN_COLLECTION_HANDLES)
        summaries = summaries[args.offset :]
        if args.limit:
            summaries = summaries[: args.limit]
        LOG.info("Selected %s STOY men's clothing and footwear products.", len(summaries))
        if args.discover_only:
            for product in summaries[: args.preview]:
                LOG.info("Discovered STOY product: %s (%s)", product.get("title"), product.get("handle"))
            return 0
        pending: list[dict] = []
        dry_run_rows: list[dict] = []
        for index, summary in enumerate(summaries, 1):
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            handle = summary.get("handle") or summary.get("url")
            LOG.info("[%s/%s] Hydrating %s", index, len(summaries), handle)
            row = scraper.product_to_row(scraper.fetch_product(handle), scraper.fetch_product_page(handle))
            if args.dry_run:
                dry_run_rows.append(row)
            else:
                pending.append(row)
                if len(pending) >= args.write_batch_size:
                    assert client is not None
                    client.upsert_products(table, pending)
                    LOG.info("Upserted %s STOY rows to %s.", len(pending), table)
                    pending.clear()
        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(dry_run_rows, ensure_ascii=False, indent=2))
        elif pending:
            assert client is not None
            client.upsert_products(table, pending)
            LOG.info("Upserted final %s STOY rows to %s.", len(pending), table)
        return 0
    except Exception:
        LOG.exception("STOY import failed")
        return 1


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Full-import STOY men's clothing and footwear with in-store availability.")
    parser.add_argument("--table", default=env("STOY_PRODUCTS_TABLE", "stoy_products"))
    parser.add_argument("--url", action="append", help="Import one STOY product URL.")
    parser.add_argument("--collection", action="append", help="Override default clothing + footwear discovery; may be repeated.")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--write-batch-size", type=int, default=int(env("STOY_WRITE_BATCH_SIZE", "50")))
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    arguments = parser.parse_args()
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_stoy_products(arguments))
