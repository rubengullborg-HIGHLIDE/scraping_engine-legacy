from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import random
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


LOG = logging.getLogger("refresh_store_inventory")
EMPTY_LOCAL_INVENTORY = {"stores": {}}


@dataclass(frozen=True)
class StoreSpec:
    key: str
    table: str
    identity_columns: tuple[str, ...]
    match_column: str
    dynamic_columns: tuple[str, ...]
    unavailable_totals_unknown: bool = False


@dataclass
class RefreshStats:
    pages: int = 0
    updated: int = 0
    unavailable: int = 0
    history_observations: int = 0
    failed: int = 0
    skipped: int = 0


COMMON_DYNAMIC_COLUMNS = (
    "current_price",
    "list_price",
    "webshop_sizes",
    "local_inventory",
    "local_available",
    "aarhus_available",
)

TOTAL_COLUMNS = (
    "local_total_stock",
    "aarhus_total_stock",
)

TIMESTAMP_COLUMNS = (
    "inventory_checked_at",
    "scraped_at",
    "updated_at",
)

STORE_SPECS: "OrderedDict[str, StoreSpec]" = OrderedDict(
    (
        (
            "rains",
            StoreSpec(
                key="rains",
                table="rains_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
            ),
        ),
        (
            "romerhus",
            StoreSpec(
                key="romerhus",
                table="romerhus_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS
                + TOTAL_COLUMNS
                + ("scraped_at", "updated_at"),
            ),
        ),
        (
            "suitclub",
            StoreSpec(
                key="suitclub",
                table="suitclub_products",
                identity_columns=("source_product_id",),
                match_column="source_product_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
            ),
        ),
        (
            "cejf",
            StoreSpec(
                key="cejf",
                table="cejf_products",
                identity_columns=("source_product_id",),
                match_column="source_product_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
                unavailable_totals_unknown=True,
            ),
        ),
        (
            "skagen_clothing",
            StoreSpec(
                key="skagen_clothing",
                table="skagen_clothing_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
            ),
        ),
        (
            "shoechapter",
            StoreSpec(
                key="shoechapter",
                table="shoechapter_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
            ),
        ),
        (
            "stoy",
            StoreSpec(
                key="stoy",
                table="stoy_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
            ),
        ),
        (
            "lakor",
            StoreSpec(
                key="lakor",
                table="lakor_products",
                identity_columns=("source_parent_id", "source_color_id"),
                match_column="source_color_id",
                dynamic_columns=COMMON_DYNAMIC_COLUMNS + ("scraped_at", "updated_at"),
            ),
        ),
    )
)


for _store in ("axel", "quint"):
    STORE_SPECS[_store] = StoreSpec(
        key=_store, table=f"{_store}_products",
        identity_columns=("source_parent_id", "source_color_id"),
        match_column="source_color_id",
        dynamic_columns=COMMON_DYNAMIC_COLUMNS + TOTAL_COLUMNS + TIMESTAMP_COLUMNS,
    )


class LocalInventoryClient:
    """Read sample existing rows without credentials or database access."""
    def __init__(self, path):
        self.rows = json.loads(Path(path).read_text())
        if not isinstance(self.rows, list) or any(not isinstance(row, dict) or type(row.get("id")) is not int for row in self.rows):
            raise ValueError("Local input must be a JSON list of existing rows with integer IDs")

    def list_rows(self, spec):
        return [row for row in self.rows if row.get("publication_status", "active") == "active"]

    def close(self):
        pass


class SupabasePatchError(RuntimeError):
    """A Supabase row patch failed and the sequential run must stop."""


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
    return default if value is None or value == "" else value


class SupabaseInventoryClient:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        *,
        history_table: str,
        runs_table: str,
        history_batch_size: int = 50,
        timeout_seconds: float = 60,
        max_retries: int = 2,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.history_table = history_table
        self.runs_table = runs_table
        self.history_batch_size = history_batch_size
        self.timeout_seconds = timeout_seconds
        self.pending_history: list[dict[str, Any]] = []
        self.session = requests.Session()
        headers = {
            "apikey": supabase_key,
            "Content-Type": "application/json",
        }
        if supabase_key.count(".") == 2:
            headers["Authorization"] = f"Bearer {supabase_key}"
        self.session.headers.update(headers)
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PATCH", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)

    def _table_url(self, table: str) -> str:
        return f"{self.supabase_url}/rest/v1/{quote(table)}"

    def list_rows(self, spec: StoreSpec, page_size: int = 1000) -> list[dict[str, Any]]:
        columns = (
            "id",
            *spec.identity_columns,
            "source_url",
            "canonical_url",
            "current_price",
            "list_price",
            "local_inventory",
            "publication_status",
            "discontinued_at",
            "updated_at",
        )
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.session.get(
                self._table_url(spec.table),
                params={
                    "select": ",".join(columns),
                    "publication_status": "eq.active",
                    "order": "updated_at.asc,id.asc",
                },
                headers={"Range": f"{start}-{start + page_size - 1}"},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            start += page_size

    def patch_row(self, spec: StoreSpec, row_id: int, payload: dict[str, Any]) -> None:
        response = self.session.patch(
            self._table_url(spec.table),
            params={"id": f"eq.{row_id}"},
            headers={"Prefer": "return=minimal"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        if response.ok:
            return
        body = (response.text or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "..."
        raise SupabasePatchError(
            f"Supabase PATCH {spec.table} id={row_id} returned HTTP "
            f"{response.status_code}: {body or '<empty response body>'}"
        )

    def queue_history_observation(self, payload: dict[str, Any]) -> None:
        self.pending_history.append(payload)
        if len(self.pending_history) >= self.history_batch_size:
            self.flush_history()

    def flush_history(self) -> None:
        if not self.pending_history:
            return
        response = self.session.post(
            self._table_url(self.history_table),
            headers={"Prefer": "return=minimal"},
            data=json.dumps(self.pending_history, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            body = (response.text or "").strip()
            if len(body) > 2000:
                body = body[:2000] + "..."
            raise SupabasePatchError(
                f"Supabase history insert into {self.history_table} returned HTTP "
                f"{response.status_code}: {body or '<empty response body>'}"
            )
        self.pending_history.clear()

    def create_refresh_run(self, payload: dict[str, Any]) -> None:
        response = self.session.post(
            self._table_url(self.runs_table),
            headers={"Prefer": "return=minimal"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            body = (response.text or "").strip()
            raise SupabasePatchError(
                f"Supabase refresh-run insert into {self.runs_table} returned HTTP "
                f"{response.status_code}: {body[:2000] or '<empty response body>'}"
            )

    def update_refresh_run(self, run_id: str, payload: dict[str, Any]) -> None:
        response = self.session.patch(
            self._table_url(self.runs_table),
            params={"id": f"eq.{run_id}"},
            headers={"Prefer": "return=minimal"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            body = (response.text or "").strip()
            raise SupabasePatchError(
                f"Supabase refresh-run update in {self.runs_table} returned HTTP "
                f"{response.status_code}: {body[:2000] or '<empty response body>'}"
            )

    def close(self) -> None:
        self.session.close()


def row_url(row: dict[str, Any]) -> str:
    value = row.get("canonical_url") or row.get("source_url")
    if not value:
        raise ValueError(f"Database row {row.get('id')} has no source URL.")
    return str(value).split("?", 1)[0].split("#", 1)[0]


def zero_inventory(source: Any) -> dict[str, Any]:
    inventory = copy.deepcopy(source) if isinstance(source, dict) else copy.deepcopy(EMPTY_LOCAL_INVENTORY)
    stores = inventory.get("stores")
    if not isinstance(stores, dict):
        return copy.deepcopy(EMPTY_LOCAL_INVENTORY)
    for store in stores.values():
        if not isinstance(store, dict):
            continue
        stock_known = bool(store.get("stock_known"))
        store["available"] = False
        store["total_stock"] = 0 if stock_known else None
        sizes = store.get("sizes")
        if not isinstance(sizes, dict):
            store["sizes"] = {}
            continue
        for size in sizes.values():
            if not isinstance(size, dict):
                continue
            size["available"] = False
            size["stock"] = 0 if stock_known else None
    return inventory


def dynamic_payload(spec: StoreSpec, full_row: dict[str, Any]) -> dict[str, Any]:
    missing = [column for column in spec.dynamic_columns if column not in full_row]
    if missing:
        raise ValueError(
            f"{spec.key} dynamic parser omitted required columns: {', '.join(missing)}"
        )
    return {column: full_row[column] for column in spec.dynamic_columns}


def history_checked_at(payload: dict[str, Any]) -> str:
    value = (
        payload.get("inventory_checked_at")
        or payload.get("scraped_at")
        or payload.get("updated_at")
    )
    if not value:
        raise ValueError("Dynamic payload has no inventory observation timestamp.")
    return str(value)


def history_payload(
    spec: StoreSpec,
    existing_row: dict[str, Any],
    dynamic: dict[str, Any],
    *,
    checked_at: str,
    refresh_status: str,
) -> dict[str, Any]:
    checked_datetime = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    checked_bucket = checked_datetime.astimezone(timezone.utc).date().isoformat()
    return {
        "store": spec.key,
        "product_id": existing_row["id"],
        "state_hash": "pending",
        "observed_from": checked_at,
        "observed_through": checked_at,
        "last_observed_bucket": checked_bucket,
        "observation_count": 1,
        "refresh_status": refresh_status,
        "current_price": dynamic.get("current_price"),
        "list_price": dynamic.get("list_price"),
        "local_inventory": dynamic.get("local_inventory") or copy.deepcopy(EMPTY_LOCAL_INVENTORY),
        "local_total_stock": dynamic.get("local_total_stock"),
        "local_available": dynamic.get("local_available"),
        "aarhus_total_stock": dynamic.get("aarhus_total_stock"),
        "aarhus_available": dynamic.get("aarhus_available"),
        "updated_at": checked_at,
    }


def unavailable_payload(
    spec: StoreSpec,
    existing_row: dict[str, Any],
    checked_at: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "current_price": existing_row.get("current_price"),
        "list_price": existing_row.get("list_price"),
        "webshop_sizes": [],
        "local_inventory": zero_inventory(existing_row.get("local_inventory")),
        "local_available": False,
        "aarhus_available": False,
        "updated_at": checked_at,
    }
    if "local_total_stock" in spec.dynamic_columns:
        payload["local_total_stock"] = None if spec.unavailable_totals_unknown else 0
    if "aarhus_total_stock" in spec.dynamic_columns:
        payload["aarhus_total_stock"] = None if spec.unavailable_totals_unknown else 0
    if "inventory_checked_at" in spec.dynamic_columns:
        payload["inventory_checked_at"] = checked_at
    if "scraped_at" in spec.dynamic_columns:
        payload["scraped_at"] = checked_at
    return {column: payload[column] for column in spec.dynamic_columns}


def explicitly_unavailable(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in {404, 410}


def ensure_identity(
    spec: StoreSpec,
    existing_row: dict[str, Any],
    full_row: dict[str, Any],
) -> None:
    expected = str(existing_row.get(spec.match_column) or "")
    actual = str(full_row.get(spec.match_column) or "")
    if not expected or expected != actual:
        raise ValueError(
            f"{spec.key} identity mismatch for database id={existing_row.get('id')}: "
            f"expected {spec.match_column}={expected!r}, got {actual!r}"
        )


def apply_full_row(
    spec: StoreSpec,
    database: SupabaseInventoryClient,
    existing_row: dict[str, Any],
    full_row: dict[str, Any],
    args: argparse.Namespace,
    stats: RefreshStats,
) -> None:
    ensure_identity(spec, existing_row, full_row)
    payload = dynamic_payload(spec, full_row)
    if args.dry_run:
        LOG.info(
            "Dry run %s id=%s price=%s aarhus_available=%s",
            spec.key,
            existing_row["id"],
            payload.get("current_price"),
            payload.get("aarhus_available"),
        )
    else:
        database.patch_row(spec, int(existing_row["id"]), payload)
        if not args.no_history:
            database.queue_history_observation(
                history_payload(
                    spec,
                    existing_row,
                    payload,
                    checked_at=history_checked_at(payload),
                    refresh_status="ok",
                )
            )
            stats.history_observations += 1
    stats.updated += 1


def apply_unavailable(
    spec: StoreSpec,
    database: SupabaseInventoryClient,
    existing_row: dict[str, Any],
    args: argparse.Namespace,
    stats: RefreshStats,
    *,
    refresh_status: str = "page_unavailable",
) -> None:
    checked_at = datetime.now(timezone.utc).isoformat()
    payload = unavailable_payload(spec, existing_row, checked_at)
    payload.update(
        {
            "publication_status": "unavailable",
            "status_reason": "source_item_missing" if refresh_status == "source_item_missing" else "page_404_or_410",
            "status_checked_at": checked_at,
            "discontinued_at": existing_row.get("discontinued_at") or checked_at,
        }
    )
    if args.dry_run:
        LOG.info("Dry run unavailable %s id=%s", spec.key, existing_row["id"])
    else:
        database.patch_row(spec, int(existing_row["id"]), payload)
        if not args.no_history:
            database.queue_history_observation(
                history_payload(
                    spec,
                    existing_row,
                    payload,
                    checked_at=checked_at,
                    refresh_status=refresh_status,
                )
            )
            stats.history_observations += 1
    stats.unavailable += 1


def maybe_delay(index: int, args: argparse.Namespace) -> None:
    if index > 1 and not args.no_delay:
        time.sleep(random.uniform(args.min_delay, args.max_delay))


def selected_rows(
    database: SupabaseInventoryClient,
    spec: StoreSpec,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    rows = database.list_rows(spec)
    if args.offset:
        rows = rows[args.offset :]
    if args.limit:
        rows = rows[: args.limit]
    return rows


def close_scraper(scraper: Any) -> None:
    session = getattr(scraper, "session", None)
    if session is not None:
        session.close()


def configure_scraper(scraper: Any, args: argparse.Namespace) -> Any:
    """Add bounded retries to the full-import scraper's read-only HTTP session."""
    session = getattr(scraper, "session", None)
    if session is None:
        return scraper
    retry = Retry(
        total=args.max_retries,
        connect=args.max_retries,
        read=args.max_retries,
        status=args.max_retries,
        backoff_factor=0.75,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return scraper


def refresh_single_row_store(
    spec: StoreSpec,
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
    scraper: Any,
    builder: Callable[[Any, dict[str, Any], argparse.Namespace], dict[str, Any]],
) -> RefreshStats:
    rows = selected_rows(database, spec, args)
    stats = RefreshStats()
    LOG.info("Loaded %s %s rows.", len(rows), spec.key)
    try:
        for index, existing_row in enumerate(rows, start=1):
            maybe_delay(index, args)
            stats.pages += 1
            url = row_url(existing_row)
            LOG.info("%s [%s/%s] Refreshing %s", spec.key, index, len(rows), url)
            try:
                full_row = builder(scraper, existing_row, args)
                apply_full_row(spec, database, existing_row, full_row, args, stats)
            except SupabasePatchError:
                raise
            except Exception as exc:
                if explicitly_unavailable(exc):
                    LOG.warning("%s is gone; marking id=%s unavailable.", url, existing_row["id"])
                    apply_unavailable(spec, database, existing_row, args, stats)
                    continue
                stats.failed += 1
                LOG.exception("Failed %s row id=%s url=%s", spec.key, existing_row["id"], url)
    finally:
        close_scraper(scraper)
    return stats


def refresh_cejf(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.cejf import CejfScraper

    return refresh_single_row_store(
        STORE_SPECS["cejf"],
        database,
        args,
        configure_scraper(CejfScraper(), args),
        lambda scraper, row, _args: scraper.product_to_row(
            scraper.fetch_product(row_url(row))
        ),
    )


def refresh_stoy(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.stoy import StoyScraper

    return refresh_single_row_store(
        STORE_SPECS["stoy"],
        database,
        args,
        configure_scraper(StoyScraper(), args),
        lambda scraper, row, _args: scraper.product_to_row(
            scraper.fetch_product(row_url(row)),
            scraper.fetch_product_page(row_url(row)),
        ),
    )


def refresh_shoechapter(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.shoechapter import ShoeChapterScraper

    return refresh_single_row_store(
        STORE_SPECS["shoechapter"],
        database,
        args,
        configure_scraper(ShoeChapterScraper(), args),
        lambda scraper, row, _args: scraper.product_to_row(
            scraper.fetch_product(row_url(row)),
            scraper.fetch_product_page(row_url(row)),
        ),
    )


def refresh_skagen_clothing(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.skagen_clothing import SkagenClothingScraper

    return refresh_single_row_store(
        STORE_SPECS["skagen_clothing"],
        database,
        args,
        configure_scraper(SkagenClothingScraper(), args),
        lambda scraper, row, _args: scraper.product_to_row(
            scraper.fetch_product(row_url(row)),
            scraper.fetch_product_page(row_url(row)),
        ),
    )


def refresh_lakor(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.lakor import LakorScraper

    return refresh_single_row_store(
        STORE_SPECS["lakor"],
        database,
        args,
        configure_scraper(LakorScraper(), args),
        lambda scraper, row, run_args: scraper.product_to_row(
            scraper.fetch_product(row_url(row)),
            "",
            variant_delay=run_args.variant_delay,
            no_delay=run_args.no_delay,
        ),
    )


def refresh_romerhus(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.romerhus import RomerhusScraper

    scraper = configure_scraper(RomerhusScraper(), args)
    try:
        locations = {
            int(location["id"]): location
            for location in scraper.fetch_store_locations()
            if location.get("id") is not None
        }
        stores = scraper._tracked_stores(locations)
    except Exception:
        close_scraper(scraper)
        raise

    def build(scraper_instance: Any, row: dict[str, Any], _args: argparse.Namespace) -> dict[str, Any]:
        product = scraper_instance.fetch_product(row_url(row))
        variant_ids = [
            int(variant["id"])
            for variant in product.get("variants", [])
            if variant.get("id") is not None
        ]
        stock = scraper_instance.fetch_variant_stock(variant_ids)
        return scraper_instance.product_to_row(product, stock, stores)

    return refresh_single_row_store(spec=STORE_SPECS["romerhus"], database=database, args=args, scraper=scraper, builder=build)


def refresh_rains(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.rains import RainsScraper

    spec = STORE_SPECS["rains"]
    rows = selected_rows(database, spec, args)
    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for row in rows:
        grouped.setdefault(row_url(row), []).append(row)
    scraper = configure_scraper(RainsScraper(), args)
    stats = RefreshStats()
    LOG.info("Loaded %s Rains rows across %s product pages.", len(rows), len(grouped))
    try:
        inventory_snapshot = scraper.fetch_inventory()
        for index, (url, page_rows) in enumerate(grouped.items(), start=1):
            maybe_delay(index, args)
            stats.pages += 1
            LOG.info("rains [%s/%s] Refreshing %s", index, len(grouped), url)
            try:
                product = scraper.fetch_product(url)
                full_rows = scraper.product_to_rows(product, "", inventory_snapshot)
                full_by_key = {
                    (str(row["source_parent_id"]), str(row["source_color_id"])): row
                    for row in full_rows
                }
                for existing_row in page_rows:
                    key = (
                        str(existing_row["source_parent_id"]),
                        str(existing_row["source_color_id"]),
                    )
                    full_row = full_by_key.get(key)
                    if full_row is None:
                        LOG.warning(
                            "Rains color disappeared from a successful feed; marking id=%s unavailable.",
                            existing_row["id"],
                        )
                        apply_unavailable(
                            spec,
                            database,
                            existing_row,
                            args,
                            stats,
                            refresh_status="source_item_missing",
                        )
                    else:
                        apply_full_row(spec, database, existing_row, full_row, args, stats)
            except SupabasePatchError:
                raise
            except Exception as exc:
                if explicitly_unavailable(exc):
                    for existing_row in page_rows:
                        apply_unavailable(spec, database, existing_row, args, stats)
                    continue
                stats.failed += len(page_rows)
                LOG.exception("Failed Rains product page %s", url)
    finally:
        close_scraper(scraper)
    return stats


def refresh_suitclub(
    database: SupabaseInventoryClient,
    args: argparse.Namespace,
) -> RefreshStats:
    from scrapers.full_import.suitclub import SuitClubScraper

    spec = STORE_SPECS["suitclub"]
    rows = selected_rows(database, spec, args)
    scraper = configure_scraper(SuitClubScraper(), args)
    stats = RefreshStats()
    page_cache: dict[int, str] = {}
    available_rows: list[dict[str, Any]] = []
    LOG.info("Loaded %s SuitClub rows.", len(rows))
    try:
        config_html: Optional[str] = None
        for existing_row in rows:
            if config_html is not None:
                available_rows.append(existing_row)
                continue
            try:
                page_html = scraper.fetch_product_page(row_url(existing_row))
                page_cache[int(existing_row["id"])] = page_html
                available_rows.append(existing_row)
                config_html = page_html
            except Exception as exc:
                stats.pages += 1
                if explicitly_unavailable(exc):
                    apply_unavailable(spec, database, existing_row, args, stats)
                else:
                    stats.failed += 1
                    LOG.exception("Failed to load SuitClub config/product page id=%s", existing_row["id"])

        if not available_rows:
            return stats
        if config_html is None:
            raise RuntimeError("Could not find a live SuitClub page with Storefront API configuration.")

        inventory_snapshot = scraper.fetch_inventory_snapshot(
            [row["source_product_id"] for row in available_rows],
            config_html,
            batch_size=args.suitclub_inventory_batch_size,
            batch_delay=0 if args.no_delay else args.suitclub_inventory_batch_delay,
        )
        for index, existing_row in enumerate(available_rows, start=1):
            maybe_delay(index, args)
            stats.pages += 1
            url = row_url(existing_row)
            LOG.info("suitclub [%s/%s] Refreshing %s", index, len(available_rows), url)
            try:
                product = scraper.fetch_product(url)
                page_html = page_cache.pop(int(existing_row["id"]), None)
                if page_html is None:
                    page_html = scraper.fetch_product_page(url)
                full_row = scraper.product_to_row(product, page_html, inventory_snapshot)
                apply_full_row(spec, database, existing_row, full_row, args, stats)
            except SupabasePatchError:
                raise
            except Exception as exc:
                if explicitly_unavailable(exc):
                    apply_unavailable(spec, database, existing_row, args, stats)
                    continue
                stats.failed += 1
                LOG.exception("Failed SuitClub row id=%s url=%s", existing_row["id"], url)
    finally:
        close_scraper(scraper)
    return stats


STORE_REFRESHERS: dict[
    str,
    Callable[[SupabaseInventoryClient, argparse.Namespace], RefreshStats],
] = {
    "rains": refresh_rains,
    "romerhus": refresh_romerhus,
    "suitclub": refresh_suitclub,
    "cejf": refresh_cejf,
    "skagen_clothing": refresh_skagen_clothing,
    "shoechapter": refresh_shoechapter,
    "stoy": refresh_stoy,
    "lakor": refresh_lakor,
}


from scripts.refresh_axel_quint_inventory import refresh_store as refresh_axel_quint
STORE_REFRESHERS.update({key: partial(refresh_axel_quint, key) for key in ("axel", "quint")})


def refresh_stores(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not getattr(args, "input", None) and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required.")

    selected = list(STORE_SPECS) if args.all or not args.store else list(OrderedDict.fromkeys(args.store))
    database = LocalInventoryClient(args.input) if getattr(args, "input", None) else SupabaseInventoryClient(
        supabase_url,
        supabase_key,
        history_table=args.history_table,
        runs_table=args.runs_table,
        history_batch_size=args.history_batch_size,
        timeout_seconds=args.supabase_timeout,
        max_retries=args.max_retries,
    )
    totals = RefreshStats()
    store_failures = 0
    stores_completed = 0
    write_failed = False
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    run_created = False
    try:
        if not args.dry_run:
            try:
                database.create_refresh_run(
                    {
                        "id": run_id,
                        "mode": "all" if args.all or not args.store else "selected",
                        "selected_stores": selected,
                        "status": "running",
                        "started_at": started_at,
                        "row_limit": args.limit,
                        "row_offset": args.offset,
                        "history_enabled": not args.no_history,
                        "stores_planned": len(selected),
                        "updated_at": started_at,
                    }
                )
                run_created = True
            except SupabasePatchError as exc:
                write_failed = True
                LOG.error("Could not create store refresh run: %s", exc)

        for store_key in selected:
            if write_failed:
                break
            LOG.info("Starting sequential %s inventory refresh.", store_key)
            try:
                stats = STORE_REFRESHERS[store_key](database, args)
                if not args.dry_run and not args.no_history:
                    database.flush_history()
            except SupabasePatchError as exc:
                LOG.error("Stopping all store refreshes after database write failure: %s", exc)
                write_failed = True
                break
            except Exception:
                if not args.dry_run and not args.no_history:
                    try:
                        database.flush_history()
                    except SupabasePatchError as exc:
                        LOG.error(
                            "Stopping all store refreshes after history write failure: %s",
                            exc,
                        )
                        write_failed = True
                        break
                store_failures += 1
                LOG.exception("Store-level refresh failure for %s", store_key)
                continue
            stores_completed += 1
            totals.pages += stats.pages
            totals.updated += stats.updated
            totals.unavailable += stats.unavailable
            totals.history_observations += stats.history_observations
            totals.failed += stats.failed
            totals.skipped += stats.skipped
            LOG.info(
                "%s refresh complete. pages=%s updated=%s unavailable=%s history_observations=%s failed=%s",
                store_key,
                stats.pages,
                stats.updated,
                stats.unavailable,
                stats.history_observations,
                stats.failed,
            )
    finally:
        if run_created:
            finished_at = datetime.now(timezone.utc).isoformat()
            status = (
                "failed"
                if write_failed
                else "partial"
                if totals.failed or store_failures
                else "succeeded"
            )
            try:
                database.update_refresh_run(
                    run_id,
                    {
                        "status": status,
                        "finished_at": finished_at,
                        "stores_completed": stores_completed,
                        "failed_stores": store_failures,
                        "pages_processed": totals.pages,
                        "updated_products": totals.updated,
                        "unavailable_products": totals.unavailable,
                        "history_observations": totals.history_observations,
                        "failed_products": totals.failed,
                        "skipped_products": totals.skipped,
                        "write_failed": write_failed,
                        "updated_at": finished_at,
                    },
                )
            except SupabasePatchError as exc:
                write_failed = True
                LOG.error("Could not finalize store refresh run %s: %s", run_id, exc)
        database.close()

    LOG.info(
        "Sequential store refresh complete. stores=%s pages=%s updated=%s "
        "unavailable=%s history_observations=%s failed_rows=%s failed_stores=%s "
        "write_failed=%s",
        len(selected),
        totals.pages,
        totals.updated,
        totals.unavailable,
        totals.history_observations,
        totals.failed,
        store_failures,
        write_failed,
    )
    return 1 if totals.failed or store_failures or write_failed else 0


def parse_args(argv=None) -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially refresh price, webshop availability, and local-store "
            "inventory for existing non-Kaufmann product rows."
        )
    )
    parser.add_argument(
        "--store",
        action="append",
        choices=list(STORE_SPECS),
        help="Refresh one store; repeat for an explicit sequence. Defaults to all stores.",
    )
    parser.add_argument("--all", action="store_true", help="Refresh every configured store sequentially.")
    parser.add_argument("--limit", type=int, help="Limit existing rows per store for testing.")
    parser.add_argument("--offset", type=int, default=0, help="Skip existing rows per store.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse without writing.")
    parser.add_argument(
        "--no-history",
        "--no-snapshots",
        dest="no_history",
        action="store_true",
        help=(
            "Patch live product rows without writing inventory history. "
            "--no-snapshots remains as a deprecated alias."
        ),
    )
    parser.add_argument(
        "--history-table",
        "--snapshots-table",
        dest="history_table",
        default=env("STORE_INVENTORY_HISTORY_TABLE", "store_inventory_history"),
        help=(
            "Change-based non-Kaufmann inventory history table. "
            "--snapshots-table remains as a deprecated alias."
        ),
    )
    parser.add_argument(
        "--history-batch-size",
        "--snapshot-batch-size",
        dest="history_batch_size",
        type=int,
        default=int(env("STORE_REFRESH_HISTORY_BATCH_SIZE", "50")),
        help=(
            "Number of observations per Supabase history insert. "
            "--snapshot-batch-size remains as a deprecated alias."
        ),
    )
    parser.add_argument(
        "--runs-table",
        default=env(
            "STORE_INVENTORY_REFRESH_RUNS_TABLE",
            "store_inventory_refresh_runs",
        ),
        help="Operational run table for sequential non-Kaufmann refreshes.",
    )
    parser.add_argument("--no-delay", action="store_true", help="Disable polite inter-product delays.")
    parser.add_argument("--min-delay", type=float, default=float(env("STORE_REFRESH_MIN_DELAY", "0.5")))
    parser.add_argument("--max-delay", type=float, default=float(env("STORE_REFRESH_MAX_DELAY", "1.0")))
    parser.add_argument("--variant-delay", type=float, default=float(env("STORE_REFRESH_VARIANT_DELAY", "0.2")))
    parser.add_argument("--supabase-timeout", type=float, default=float(env("STORE_REFRESH_SUPABASE_TIMEOUT", "60")))
    parser.add_argument("--max-retries", type=int, default=int(env("STORE_REFRESH_MAX_RETRIES", "2")))
    parser.add_argument("--suitclub-inventory-batch-size", type=int, default=10)
    parser.add_argument("--suitclub-inventory-batch-delay", type=float, default=0.5)
    parser.add_argument(
        "--log-level",
        default=env("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    parser.add_argument("--input", help="Local existing-row JSON for AXEL/qUINT dry runs; no database access.")
    arguments = parser.parse_args(argv)
    if arguments.input and (not arguments.dry_run or arguments.all or arguments.store not in (["axel"], ["quint"])):
        parser.error("--input requires --dry-run and exactly one --store axel or quint")
    if arguments.all and arguments.store:
        parser.error("use either --all or one or more --store values")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")
    if arguments.offset < 0:
        parser.error("--offset cannot be negative")
    if arguments.min_delay < 0 or arguments.max_delay < arguments.min_delay:
        parser.error("delay values must satisfy 0 <= min-delay <= max-delay")
    if arguments.variant_delay < 0:
        parser.error("--variant-delay cannot be negative")
    if arguments.supabase_timeout <= 0:
        parser.error("--supabase-timeout must be greater than 0")
    if arguments.max_retries < 0:
        parser.error("--max-retries cannot be negative")
    if arguments.history_batch_size < 1:
        parser.error("--history-batch-size must be at least 1")
    if arguments.suitclub_inventory_batch_size < 1:
        parser.error("--suitclub-inventory-batch-size must be at least 1")
    if arguments.suitclub_inventory_batch_delay < 0:
        parser.error("--suitclub-inventory-batch-delay cannot be negative")
    return arguments


if __name__ == "__main__":
    cli_args = parse_args()
    logging.basicConfig(
        level=getattr(logging, cli_args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(refresh_stores(cli_args))
