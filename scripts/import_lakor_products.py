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

LOG = logging.getLogger("import_lakor_products")


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


class SupabaseCatalogClient:
    def __init__(self, supabase_url: str, supabase_key: str) -> None:
        import requests

        self.supabase_url = supabase_url.rstrip("/")
        self.session = requests.Session()
        headers = {"apikey": supabase_key, "Content-Type": "application/json"}
        if supabase_key.count(".") == 2:
            headers["Authorization"] = f"Bearer {supabase_key}"
        self.session.headers.update(headers)

    def upsert_products(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        response = self.session.post(
            f"{self.supabase_url}/rest/v1/{quote(table)}",
            params={"on_conflict": "source_parent_id,source_color_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
        )
        response.raise_for_status()


def import_lakor_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.lakor import CLOTHING_COLLECTION_HANDLE, LakorScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("LAKOR_PRODUCTS_TABLE", "lakor_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = LakorScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    try:
        summaries = [{"handle": url} for url in args.url] if args.url else scraper.discover_products(args.collection or CLOTHING_COLLECTION_HANDLE)
        summaries = summaries[args.offset :]
        if args.limit:
            summaries = summaries[: args.limit]
        LOG.info("Discovered %s unique LAKOR collection product candidates.", len(summaries))
        if args.discover_only:
            for product in summaries[: args.preview]:
                LOG.info("Discovered collection product: %s (%s)", product.get("title"), product.get("handle"))
            return 0

        pending_handles = [
            scraper._handle_from_url(summary.get("handle") or summary.get("url"))
            for summary in summaries
            if summary.get("handle") or summary.get("url")
        ]
        queued_handles = {handle for handle in pending_handles if handle}
        products: list[tuple[dict[str, Any], str]] = []
        index = 0
        while pending_handles:
            handle = pending_handles.pop(0)
            if not handle:
                continue
            index += 1
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            LOG.info("[%s] Hydrating %s", index, handle)
            product = scraper.fetch_product(handle)
            if not scraper.is_clothing(product):
                LOG.info("Skipping non-clothing product: %s (%s)", product.get("title"), product.get("type"))
                continue
            page_html = scraper.fetch_product_page(handle)
            products.append((product, page_html))
            for sibling_handle in scraper.color_handles(page_html):
                if sibling_handle not in queued_handles:
                    queued_handles.add(sibling_handle)
                    pending_handles.append(sibling_handle)
        LOG.info("Selected %s LAKOR clothing colour products for import.", len(products))
        rows = scraper.build_catalog_rows(products, variant_delay=args.variant_delay, no_delay=args.no_delay)
        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            assert client is not None
            client.upsert_products(table, rows)
        LOG.info("LAKOR import complete. rows=%s", len(rows))
        return 0
    except Exception:
        LOG.exception("LAKOR import failed")
        return 1


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Full-import LAKOR clothing and public local-store availability.")
    parser.add_argument("--table", default=env("LAKOR_PRODUCTS_TABLE", "lakor_products"))
    parser.add_argument("--url", action="append", help="Import one product URL.")
    parser.add_argument("--collection", help="Override the default all-clothing collection handle.")
    parser.add_argument("--limit", type=int, help="Limit discovered product colours for testing.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many discovered product colours.")
    parser.add_argument("--discover-only", action="store_true", help="Only list discovered product colours.")
    parser.add_argument("--preview", type=int, default=10, help="Number of discovered products to log.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape without writing to Supabase.")
    parser.add_argument("--no-delay", action="store_true", help="Disable polite delays; use only for a small test.")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument("--variant-delay", type=float, default=float(env("LAKOR_VARIANT_DELAY", "0.2")))
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_lakor_products(arguments))
