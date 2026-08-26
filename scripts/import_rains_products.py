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

LOG = logging.getLogger("import_rains_products")


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

    def upsert_products(
        self,
        table: str,
        rows: list[dict[str, Any]],
        *,
        on_conflict: str = "source_parent_id,source_color_id",
    ) -> None:
        if not rows:
            return
        response = self.session.post(
            f"{self.supabase_url}/rest/v1/{quote(table)}",
            params={"on_conflict": on_conflict},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=60,
        )
        if not response.ok:
            raise RuntimeError(f"Supabase upsert failed ({response.status_code}): {response.text[:1000]}")


def import_rains_products(args: argparse.Namespace) -> int:
    from scrapers.full_import.rains import MEN_COLLECTION_HANDLES, RainsScraper

    load_dotenv(ROOT / ".env")
    table = args.table or env("RAINS_PRODUCTS_TABLE", "rains_products")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    scraper = RainsScraper()
    client = SupabaseCatalogClient(supabase_url, supabase_key) if not args.dry_run else None
    try:
        if args.url:
            summaries = [{"handle": url, "_collection_handles": []} for url in args.url]
        else:
            summaries = scraper.discover_products(args.collection or MEN_COLLECTION_HANDLES)
        summaries = summaries[args.offset :]
        if args.limit:
            summaries = summaries[: args.limit]
        LOG.info("Selected %s unique Rains men's clothing styles.", len(summaries))

        if args.discover_only:
            for product in summaries[: args.preview]:
                LOG.info(
                    "Discovered style: %s (%s) collections=%s",
                    product.get("title"),
                    product.get("handle"),
                    ",".join(product.get("_collection_handles") or []),
                )
            return 0

        inventory_snapshot = scraper.fetch_inventory()
        LOG.info(
            "Fetched exact Rains inventory once for warehouses: %s",
            ", ".join(
                warehouse.get("name") or str(warehouse_id)
                for warehouse_id, warehouse in inventory_snapshot["warehouses"].items()
            ),
        )

        pending_rows: list[dict[str, Any]] = []
        dry_run_rows: list[dict[str, Any]] = []
        total_rows = 0
        for index, summary in enumerate(summaries, start=1):
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            handle = summary.get("handle") or summary.get("url")
            LOG.info("[%s/%s] Hydrating %s", index, len(summaries), handle)
            product = scraper.fetch_product(handle)
            product["_collection_handles"] = summary.get("_collection_handles") or []
            page_html = scraper.fetch_product_page(handle)
            rows = scraper.product_to_rows(product, page_html, inventory_snapshot)
            total_rows += len(rows)
            LOG.info("[%s/%s] Built %s colour rows (total=%s)", index, len(summaries), len(rows), total_rows)

            if args.dry_run:
                dry_run_rows.extend(rows)
                continue

            assert client is not None
            pending_rows.extend(rows)
            while len(pending_rows) >= args.write_batch_size:
                batch = pending_rows[: args.write_batch_size]
                client.upsert_products(table, batch)
                del pending_rows[: args.write_batch_size]
                LOG.info("Upserted %s Rains rows to %s.", len(batch), table)

        if args.dry_run:
            LOG.info("Dry run rows:\n%s", json.dumps(dry_run_rows, ensure_ascii=False, indent=2))
        elif pending_rows:
            assert client is not None
            client.upsert_products(table, pending_rows)
            LOG.info("Upserted final %s Rains rows to %s.", len(pending_rows), table)

        LOG.info("Rains import complete. styles=%s colour_rows=%s", len(summaries), total_rows)
        return 0
    except Exception:
        LOG.exception("Rains import failed")
        return 1


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Full-import Rains men's clothing and exact Danish store stock.")
    parser.add_argument("--table", default=env("RAINS_PRODUCTS_TABLE", "rains_products"))
    parser.add_argument("--url", action="append", help="Import one product URL; all colours on the page are retained.")
    parser.add_argument("--collection", action="append", help="Override discovery with one or more collection handles.")
    parser.add_argument("--limit", type=int, help="Limit discovered product styles for testing.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many discovered product styles.")
    parser.add_argument("--discover-only", action="store_true", help="Only list discovered product styles.")
    parser.add_argument("--preview", type=int, default=10, help="Number of discovered styles to log.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape without writing to Supabase.")
    parser.add_argument("--no-delay", action="store_true", help="Disable polite product-page delays; use only for a small test.")
    parser.add_argument("--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0")))
    parser.add_argument(
        "--write-batch-size",
        type=int,
        default=int(env("RAINS_WRITE_BATCH_SIZE", "50")),
        help="Number of colour rows per Supabase upsert.",
    )
    parser.add_argument("--log-level", default=env("LOG_LEVEL", "INFO"), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    arguments = parser.parse_args()
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(level=getattr(logging, arguments.log_level), format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(import_rains_products(arguments))
