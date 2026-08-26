from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers.stores.kaufmann_variations import (
    KaufmannVariationClient,
    KaufmannVariationUnavailable,
    clean_product_url,
    variation_rows_from_payload,
)


LOG = logging.getLogger("refresh_kaufmann_inventory")

DYNAMIC_PRODUCT_COLUMNS = (
    "current_price",
    "list_price",
    "webshop_sizes",
    "aarhus_inventory",
    "aarhus_total_stock",
    "aarhus_available",
    "source_available",
    "publication_status",
    "status_reason",
    "status_checked_at",
    "discontinued_at",
    "last_inventory_checked_at",
    "consecutive_source_misses",
    "last_refresh_error",
    "last_refresh_error_at",
    "scraped_at",
    "updated_at",
)

SNAPSHOT_COLUMNS = (
    "kaufmann_product_id",
    "source_parent_id",
    "source_color_id",
    "canonical_url",
    "source_url",
    "checked_at",
    "checked_bucket",
    "refresh_status",
    "current_price",
    "list_price",
    "webshop_sizes",
    "aarhus_inventory",
    "aarhus_total_stock",
    "aarhus_available",
    "source_available",
    "publication_status",
    "status_reason",
    "updated_at",
)

EMPTY_AARHUS_INVENTORY = {"stores": {}}
MISSING_COLOR_CONFIRMATIONS = 2


class SupabaseWriteError(RuntimeError):
    """A Supabase batch write failed and the refresh must stop safely."""


def uniform_key_batches(
    rows: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Partition PostgREST bulk rows so every object has identical keys."""
    grouped: "OrderedDict[tuple[str, ...], list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(tuple(sorted(row)), []).append(row)
    return list(grouped.values())


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


class SupabaseKaufmannRefreshClient:
    def __init__(self, supabase_url: str, supabase_key: str, timeout_seconds: float = 60):
        try:
            import requests
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "Install dependencies first: python3 -m pip install -r requirements.txt"
            ) from exc

        self.supabase_url = supabase_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        headers = {
            "apikey": supabase_key,
            "Content-Type": "application/json",
        }
        if supabase_key.count(".") == 2:
            headers["Authorization"] = f"Bearer {supabase_key}"
        self.session.headers.update(headers)

    def _table_url(self, table: str) -> str:
        return f"{self.supabase_url}/rest/v1/{quote(table)}"

    @staticmethod
    def _raise_write_error(response: Any, table: str) -> None:
        if response.ok:
            return
        body = (response.text or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "..."
        raise SupabaseWriteError(
            f"Supabase write to {table} returned HTTP {response.status_code}: "
            f"{body or '<empty response body>'}"
        )

    def list_existing_variants(
        self,
        table: str,
        *,
        include_unavailable: bool = False,
        page_size: int = 1000,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        select = ",".join(
            (
                "id",
                "source_parent_id",
                "source_color_id",
                "source_url",
                "canonical_url",
                "source_available",
                "publication_status",
                "status_reason",
                "discontinued_at",
                "last_inventory_checked_at",
                "consecutive_source_misses",
                "updated_at",
            )
        )

        while True:
            end = start + page_size - 1
            params = {
                "select": select,
                "order": "last_inventory_checked_at.asc.nullsfirst,updated_at.asc,id.asc",
            }
            if not include_unavailable:
                params["publication_status"] = "eq.active"
            response = self.session.get(
                self._table_url(table),
                params=params,
                headers={"Range": f"{start}-{end}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            start += page_size

    def upsert_products_by_id(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        response = self.session.post(
            self._table_url(table),
            params={"on_conflict": "id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        self._raise_write_error(response, table)

    def upsert_snapshots(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        response = self.session.post(
            self._table_url(table),
            params={"on_conflict": "kaufmann_product_id,checked_bucket"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        self._raise_write_error(response, table)

    def close(self) -> None:
        self.session.close()


def product_pages_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    pages: "OrderedDict[str, dict[str, str]]" = OrderedDict()
    for row in rows:
        canonical_url = row.get("canonical_url")
        source_parent_id = row.get("source_parent_id") or ""
        if not canonical_url:
            continue
        key = source_parent_id or canonical_url
        pages.setdefault(
            key,
            {
                "canonical_url": canonical_url,
                "source_parent_id": source_parent_id,
            },
        )
    return list(pages.values())


def rows_by_source_parent_id(
    rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source_parent_id = row.get("source_parent_id")
        if source_parent_id:
            grouped.setdefault(str(source_parent_id), []).append(row)
    return grouped


def dynamic_product_payload(scraped_row: dict[str, Any]) -> dict[str, Any]:
    return {
        column: scraped_row[column]
        for column in DYNAMIC_PRODUCT_COLUMNS
        if column in scraped_row
    }


def dynamic_product_upsert_row(
    product_row: dict[str, Any],
    scraped_row: dict[str, Any],
) -> dict[str, Any]:
    dynamic = dynamic_product_payload(scraped_row)
    if scraped_row.get("source_available") is False:
        dynamic["discontinued_at"] = (
            product_row.get("discontinued_at") or scraped_row.get("discontinued_at")
        )
    return {
        "id": product_row["id"],
        "source_parent_id": product_row["source_parent_id"],
        "source_color_id": product_row["source_color_id"],
        "source_url": product_row["source_url"],
        **dynamic,
    }


def snapshot_payload(
    product_row: dict[str, Any],
    scraped_row: dict[str, Any],
    checked_at: str,
    refresh_status: str = "ok",
) -> dict[str, Any]:
    payload = {
        "kaufmann_product_id": product_row["id"],
        "source_parent_id": product_row["source_parent_id"],
        "source_color_id": product_row["source_color_id"],
        "canonical_url": product_row.get("canonical_url"),
        "source_url": product_row.get("source_url"),
        "checked_at": checked_at,
        "checked_bucket": checked_at[:10],
        "refresh_status": refresh_status,
        "updated_at": checked_at,
        "current_price": scraped_row.get("current_price"),
        "list_price": scraped_row.get("list_price"),
        "webshop_sizes": scraped_row.get("webshop_sizes") or [],
        "aarhus_inventory": scraped_row.get("aarhus_inventory")
        or EMPTY_AARHUS_INVENTORY,
        "aarhus_total_stock": scraped_row.get("aarhus_total_stock") or 0,
        "aarhus_available": bool(scraped_row.get("aarhus_available")),
        "source_available": scraped_row.get("source_available"),
        "publication_status": scraped_row.get("publication_status"),
        "status_reason": scraped_row.get("status_reason"),
    }
    return {column: payload[column] for column in SNAPSHOT_COLUMNS}


def unavailable_product_payload(
    product_row: dict[str, Any],
    checked_at: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "source_available": False,
        "publication_status": "unavailable",
        "status_reason": reason,
        "status_checked_at": checked_at,
        "discontinued_at": product_row.get("discontinued_at") or checked_at,
        "last_inventory_checked_at": checked_at,
        "consecutive_source_misses": 0,
        "last_refresh_error": None,
        "last_refresh_error_at": None,
        "webshop_sizes": [],
        "aarhus_inventory": EMPTY_AARHUS_INVENTORY,
        "aarhus_total_stock": 0,
        "aarhus_available": False,
        "scraped_at": checked_at,
        "updated_at": checked_at,
    }


def unavailable_product_upsert_row(
    product_row: dict[str, Any],
    checked_at: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "id": product_row["id"],
        "source_parent_id": product_row["source_parent_id"],
        "source_color_id": product_row["source_color_id"],
        "source_url": product_row["source_url"],
        **unavailable_product_payload(product_row, checked_at, reason),
    }


def unavailable_snapshot_payload(
    product_row: dict[str, Any],
    checked_at: str,
    reason: str,
    *,
    refresh_status: str = "page_unavailable",
) -> dict[str, Any]:
    unavailable = unavailable_product_payload(product_row, checked_at, reason)
    return snapshot_payload(
        product_row,
        unavailable,
        checked_at,
        refresh_status=refresh_status,
    )


def missing_color_product_payload(
    product_row: dict[str, Any],
    checked_at: str,
) -> tuple[dict[str, Any], bool]:
    misses = int(product_row.get("consecutive_source_misses") or 0) + 1
    confirmed = misses >= MISSING_COLOR_CONFIRMATIONS
    payload: dict[str, Any] = {
        "consecutive_source_misses": misses,
        "status_checked_at": checked_at,
        "last_refresh_error": None,
        "last_refresh_error_at": None,
        "updated_at": checked_at,
    }
    if confirmed:
        payload.update(
            unavailable_product_payload(
                product_row,
                checked_at,
                "color_missing_from_variation_twice",
            )
        )
        payload["consecutive_source_misses"] = misses
    return payload, confirmed


def partial_product_upsert_row(
    product_row: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": product_row["id"],
        "source_parent_id": product_row["source_parent_id"],
        "source_color_id": product_row["source_color_id"],
        "source_url": product_row["source_url"],
        **payload,
    }


def refresh_error_payload(message: str, checked_at: str) -> dict[str, Any]:
    return {
        "last_refresh_error": message[:1000],
        "last_refresh_error_at": checked_at,
    }


def refresh_status_for_row(scraped_row: dict[str, Any]) -> str:
    return "ok" if scraped_row.get("source_available") is True else "source_unavailable"


def refresh_kaufmann_inventory(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    products_table = args.products_table or env(
        "KAUFMANN_PRODUCTS_TABLE", "kaufmann_products"
    )
    snapshots_table = args.snapshots_table or env(
        "KAUFMANN_INVENTORY_SNAPSHOTS_TABLE",
        "kaufmann_inventory_snapshots",
    )
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")

    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required for writes.")

    client = (
        SupabaseKaufmannRefreshClient(supabase_url, supabase_key)
        if supabase_url and supabase_key
        else None
    )
    variation_client = KaufmannVariationClient(
        timeout_seconds=args.request_timeout,
        max_retries=args.max_retries,
    )

    refreshed_variants = 0
    unavailable_variants = 0
    missing_variants = 0
    skipped_new_variants = 0
    failed_pages = 0
    stopped_early = False
    write_failed = False
    started_monotonic = time.monotonic()
    pending_product_updates: list[dict[str, Any]] = []
    pending_snapshots: list[dict[str, Any]] = []

    def flush_pending() -> None:
        if args.dry_run or client is None:
            return
        for batch in uniform_key_batches(pending_product_updates):
            client.upsert_products_by_id(products_table, batch)
            signature = tuple(sorted(batch[0]))
            pending_product_updates[:] = [
                row
                for row in pending_product_updates
                if tuple(sorted(row)) != signature
            ]
        for batch in uniform_key_batches(pending_snapshots):
            client.upsert_snapshots(snapshots_table, batch)
            signature = tuple(sorted(batch[0]))
            pending_snapshots[:] = [
                row for row in pending_snapshots if tuple(sorted(row)) != signature
            ]

    try:
        if args.url:
            requested_urls = {clean_product_url(url) for url in args.url}
            if client is None:
                existing_rows: list[dict[str, Any]] = []
                pages = [
                    {"canonical_url": url, "source_parent_id": ""}
                    for url in sorted(requested_urls)
                ]
            else:
                existing_rows = client.list_existing_variants(
                    products_table,
                    include_unavailable=True,
                )
                pages = [
                    page
                    for page in product_pages_from_rows(existing_rows)
                    if page["canonical_url"] in requested_urls
                ]
                if args.dry_run:
                    matched_urls = {page["canonical_url"] for page in pages}
                    pages.extend(
                        {"canonical_url": url, "source_parent_id": ""}
                        for url in sorted(requested_urls - matched_urls)
                    )
        else:
            if client is None:
                raise RuntimeError(
                    "SUPABASE_URL and SUPABASE_SECRET_KEY are required unless "
                    "--dry-run uses --url."
                )
            existing_rows = client.list_existing_variants(
                products_table,
                include_unavailable=args.include_unavailable,
            )
            pages = product_pages_from_rows(existing_rows)

        if args.offset:
            pages = pages[args.offset :]
        if args.limit:
            pages = pages[: args.limit]

        parent_rows = rows_by_source_parent_id(existing_rows)
        LOG.info(
            "Loaded %s Kaufmann product pages for HTTP variation refresh%s.",
            len(pages),
            " (including unavailable)" if args.include_unavailable else "",
        )

        for index, page in enumerate(pages, start=1):
            if args.max_runtime_minutes and (
                time.monotonic() - started_monotonic
            ) >= args.max_runtime_minutes * 60:
                stopped_early = True
                LOG.warning(
                    "Stopping cleanly after reaching the %.1f minute runtime budget.",
                    args.max_runtime_minutes,
                )
                break

            canonical_url = page["canonical_url"]
            source_parent_id = page.get("source_parent_id") or ""
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))

            checked_at = datetime.now(timezone.utc).isoformat()
            product_rows = parent_rows.get(source_parent_id, []) if source_parent_id else []
            try:
                if not source_parent_id:
                    source_parent_id = variation_client.resolve_source_parent_id(
                        canonical_url
                    )
                    product_rows = parent_rows.get(source_parent_id, [])

                LOG.info(
                    "[%s/%s] Refreshing %s via parent=%s",
                    index,
                    len(pages),
                    canonical_url,
                    source_parent_id,
                )
                payload = variation_client.fetch_payload(source_parent_id)
                scraped_rows = variation_rows_from_payload(
                    payload,
                    source_parent_id,
                    canonical_url,
                    checked_at=checked_at,
                )

                product_rows_by_color = {
                    str(row["source_color_id"]): row for row in product_rows
                }
                returned_colors: set[str] = set()

                for scraped_row in scraped_rows:
                    source_color_id = str(scraped_row["source_color_id"])
                    returned_colors.add(source_color_id)
                    product_row = product_rows_by_color.get(source_color_id)
                    if not product_row:
                        if args.dry_run and client is None:
                            LOG.info(
                                "Dry run scraped unmatched variant: %s",
                                json.dumps(
                                    dynamic_product_payload(scraped_row),
                                    ensure_ascii=False,
                                ),
                            )
                            refreshed_variants += 1
                            if scraped_row.get("source_available") is False:
                                unavailable_variants += 1
                            continue
                        skipped_new_variants += 1
                        LOG.warning(
                            "Skipping new Kaufmann variant not already in table: "
                            "parent=%s color=%s url=%s",
                            source_parent_id,
                            source_color_id,
                            canonical_url,
                        )
                        continue

                    if args.dry_run:
                        LOG.info(
                            "Dry run dynamic update for id=%s: %s",
                            product_row["id"],
                            json.dumps(
                                dynamic_product_payload(scraped_row),
                                ensure_ascii=False,
                            ),
                        )
                    else:
                        pending_product_updates.append(
                            dynamic_product_upsert_row(product_row, scraped_row)
                        )
                        pending_snapshots.append(
                            snapshot_payload(
                                product_row,
                                scraped_row,
                                checked_at,
                                refresh_status=refresh_status_for_row(scraped_row),
                            )
                        )
                    refreshed_variants += 1
                    if scraped_row.get("source_available") is False:
                        unavailable_variants += 1

                for product_row in product_rows:
                    source_color_id = str(product_row["source_color_id"])
                    if source_color_id in returned_colors:
                        continue
                    missing_payload, confirmed = missing_color_product_payload(
                        product_row,
                        checked_at,
                    )
                    missing_variants += 1
                    if args.dry_run:
                        LOG.warning(
                            "Dry run missing color update for id=%s confirmed=%s: %s",
                            product_row["id"],
                            confirmed,
                            json.dumps(missing_payload, ensure_ascii=False),
                        )
                    else:
                        pending_product_updates.append(
                            partial_product_upsert_row(product_row, missing_payload)
                        )
                        if confirmed:
                            pending_snapshots.append(
                                unavailable_snapshot_payload(
                                    product_row,
                                    checked_at,
                                    "color_missing_from_variation_twice",
                                    refresh_status="color_missing",
                                )
                            )
                    if confirmed:
                        unavailable_variants += 1

            except KaufmannVariationUnavailable as exc:
                reason = f"variation_http_{exc.status_code}"
                LOG.warning(
                    "Variation endpoint unavailable for %s. Marking %s variants unavailable.",
                    canonical_url,
                    len(product_rows),
                )
                for product_row in product_rows:
                    if args.dry_run:
                        LOG.info(
                            "Dry run unavailable update for id=%s: %s",
                            product_row["id"],
                            json.dumps(
                                unavailable_product_payload(
                                    product_row,
                                    checked_at,
                                    reason,
                                ),
                                ensure_ascii=False,
                            ),
                        )
                    else:
                        pending_product_updates.append(
                            unavailable_product_upsert_row(
                                product_row,
                                checked_at,
                                reason,
                            )
                        )
                        pending_snapshots.append(
                            unavailable_snapshot_payload(
                                product_row,
                                checked_at,
                                reason,
                            )
                        )
                    unavailable_variants += 1
            except Exception as exc:
                failed_pages += 1
                LOG.exception("Failed to refresh Kaufmann product page %s", canonical_url)
                if product_rows and not args.dry_run:
                    error = refresh_error_payload(str(exc), checked_at)
                    pending_product_updates.extend(
                        partial_product_upsert_row(product_row, error)
                        for product_row in product_rows
                    )

            if len(pending_product_updates) >= args.write_batch_size:
                try:
                    flush_pending()
                except SupabaseWriteError as exc:
                    failed_pages += 1
                    stopped_early = True
                    write_failed = True
                    LOG.error(
                        "Stopping Kaufmann refresh after a Supabase write failure: %s",
                        exc,
                    )
                    break

        if not args.dry_run and not write_failed:
            try:
                flush_pending()
            except SupabaseWriteError as exc:
                failed_pages += 1
                stopped_early = True
                write_failed = True
                LOG.error(
                    "Failed to flush final Kaufmann refresh batch: %s",
                    exc,
                )

    finally:
        variation_client.close()
        if client is not None:
            client.close()

    LOG.info(
        "Kaufmann refresh complete. refreshed_variants=%s unavailable_variants=%s "
        "missing_variants=%s skipped_new_variants=%s failed_pages=%s "
        "stopped_early=%s write_failed=%s",
        refreshed_variants,
        unavailable_variants,
        missing_variants,
        skipped_new_variants,
        failed_pages,
        stopped_early,
        write_failed,
    )
    return 1 if failed_pages else 0


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Refresh Kaufmann price, inventory, and publication status through "
            "the public variation endpoint without Playwright."
        )
    )
    parser.add_argument(
        "--products-table",
        default=env("KAUFMANN_PRODUCTS_TABLE", "kaufmann_products"),
    )
    parser.add_argument(
        "--snapshots-table",
        default=env(
            "KAUFMANN_INVENTORY_SNAPSHOTS_TABLE",
            "kaufmann_inventory_snapshots",
        ),
    )
    parser.add_argument("--url", action="append", help="Refresh a specific Kaufmann product URL.")
    parser.add_argument("--limit", type=int, help="Limit product pages for testing.")
    parser.add_argument("--offset", type=int, default=0, help="Skip this many product pages.")
    parser.add_argument("--dry-run", action="store_true", help="Scrape without writing to Supabase.")
    parser.add_argument(
        "--include-unavailable",
        action="store_true",
        help="Also recheck soft-tombstoned products; intended for weekly classification sweeps.",
    )
    parser.add_argument("--no-delay", action="store_true", help="Disable polite delay for local tests.")
    parser.add_argument(
        "--min-delay",
        type=float,
        default=float(env("KAUFMANN_REFRESH_MIN_DELAY", "1.5")),
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=float(env("KAUFMANN_REFRESH_MAX_DELAY", "3.0")),
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=float(env("KAUFMANN_REFRESH_REQUEST_TIMEOUT", "30")),
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=int(env("KAUFMANN_REFRESH_MAX_RETRIES", "2")),
    )
    parser.add_argument(
        "--write-batch-size",
        type=int,
        default=int(env("KAUFMANN_REFRESH_WRITE_BATCH_SIZE", "50")),
    )
    parser.add_argument(
        "--max-runtime-minutes",
        type=float,
        default=float(env("KAUFMANN_REFRESH_MAX_RUNTIME_MINUTES", "0")),
        help="Stop cleanly after this many minutes; 0 disables the budget.",
    )
    parser.add_argument(
        "--log-level",
        default=env("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    arguments = parser.parse_args()
    if arguments.min_delay < 0 or arguments.max_delay < arguments.min_delay:
        parser.error("delay values must satisfy 0 <= min-delay <= max-delay")
    if arguments.request_timeout <= 0:
        parser.error("--request-timeout must be greater than 0")
    if arguments.max_retries < 0:
        parser.error("--max-retries cannot be negative")
    if arguments.write_batch_size < 1:
        parser.error("--write-batch-size must be at least 1")
    if arguments.max_runtime_minutes < 0:
        parser.error("--max-runtime-minutes cannot be negative")
    return arguments


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(refresh_kaufmann_inventory(arguments))
