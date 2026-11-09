"""Shared full-import runner for Shopify stores built on ``scrapers.shopify``.

A store script supplies how to discover products and how to turn one product
into a row (``hydrate``); this module owns everything else, identically for
every store: arguments, manifest batches, per-product outcomes, delays,
dry-run output and batched upserts on ``source_product_id``.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.catalog_manifest import add_manifest_argument, manifest_items
from scripts.import_outcomes import EXIT_FAILED, ItemOutcomes
from scripts.import_rains_products import SupabaseCatalogClient, env, load_dotenv


@dataclass
class ShopifyStore:
    key: str                      # e.g. "takeoff"; also the manifest store key
    label: str                    # human name for logs
    default_table: str            # e.g. "takeoff_products"
    make_scraper: Callable[[], Any]
    # (scraper, product, args) -> row; product is a feed entry or .js payload
    hydrate: Callable[[Any, dict[str, Any], argparse.Namespace], dict[str, Any]]
    example_url: str


def parse_args(store: ShopifyStore, argv=None) -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description=f"Full-import {store.label} into {store.default_table}.")
    add_manifest_argument(parser)
    parser.add_argument("--table", default=env(f"{store.key.upper()}_PRODUCTS_TABLE", store.default_table))
    parser.add_argument("--url", action="append", help=f"Import one product URL or handle, e.g. {store.example_url}")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", help="With --dry-run, write rows to this JSON file instead of the log.")
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--variant-delay", type=float, default=0.3,
                        help="Pause between per-size requests, for stores that need them.")
    parser.add_argument("--write-batch-size", type=int, default=50)
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)
    if args.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    return args


def run_import(store: ShopifyStore, args: argparse.Namespace) -> int:
    log = logging.getLogger(f"import_{store.key}_products")
    url = env("SUPABASE_URL")
    key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not url or not key):
        log.error("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")
        return EXIT_FAILED
    scraper = store.make_scraper()
    client = SupabaseCatalogClient(url, key) if not args.dry_run else None
    outcomes = ItemOutcomes(store.key, log)
    try:
        if args.url:
            products = [scraper.fetch_product(item) for item in args.url]
        else:
            products = manifest_items(args, store.key, scraper.discover_products)
        products = products[args.offset:]
        if args.limit:
            products = products[: args.limit]
        log.info("Selected %s %s products.", len(products), store.label)
        if args.discover_only:
            for product in products[: args.preview]:
                log.info("Discovered: %s (%s) type=%s", product.get("title"), product.get("handle"),
                         product.get("product_type") or product.get("type"))
            return 0

        refetch = bool(getattr(args, "catalog_manifest", None))
        pending: list[dict[str, Any]] = []
        dry_rows: list[dict[str, Any]] = []
        for index, summary in enumerate(products, start=1):
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            handle = str(summary.get("handle") or summary.get("id"))
            log.info("[%s/%s] %s", index, len(products), handle)
            with outcomes.item(handle):
                # A frozen weekly manifest can be days old: read live prices.
                product = scraper.fetch_product(handle) if refetch else summary
                if refetch:
                    product["_discovery_collections"] = summary.get("_discovery_collections") or []
                if not scraper.is_catalog_product(product):
                    log.info("Skipping out-of-scope product %s (%s)", handle,
                             product.get("product_type") or product.get("type"))
                    continue
                (dry_rows if args.dry_run else pending).append(store.hydrate(scraper, product, args))
            if not args.dry_run and len(pending) >= args.write_batch_size:
                client.upsert_products(args.table, pending, on_conflict="source_product_id")
                log.info("Upserted %s rows to %s.", len(pending), args.table)
                pending.clear()
        if args.dry_run:
            rendered = json.dumps(dry_rows, ensure_ascii=False, indent=2)
            if args.output:
                Path(args.output).write_text(rendered, encoding="utf-8")
                log.info("Wrote %s dry-run rows to %s.", len(dry_rows), args.output)
            else:
                log.info("Dry run rows:\n%s", rendered)
        elif pending:
            client.upsert_products(args.table, pending, on_conflict="source_product_id")
            log.info("Upserted final %s rows to %s.", len(pending), args.table)
        log.info("%s import complete. imported=%s gone=%s failed=%s", store.label,
                 outcomes.succeeded, len(outcomes.gone), len(outcomes.failed))
        return outcomes.exit_code()
    except Exception:
        log.exception("%s import failed", store.label)
        return EXIT_FAILED
    finally:
        scraper.close()


def main(store: ShopifyStore, argv=None) -> int:
    args = parse_args(store, argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")
    return run_import(store, args)
