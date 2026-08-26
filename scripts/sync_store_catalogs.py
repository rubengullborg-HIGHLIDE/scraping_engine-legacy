from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.refresh_store_inventory import (  # noqa: E402
    STORE_SPECS,
    SupabasePatchError,
    snapshot_payload,
    unavailable_payload,
)


LOG = logging.getLogger("sync_store_catalogs")


@dataclass(frozen=True)
class CatalogSpec:
    key: str
    import_script: str


@dataclass
class CatalogStats:
    rows_before: int = 0
    rows_after: int = 0
    seen: int = 0
    new: int = 0
    reactivated: int = 0
    first_misses: int = 0
    confirmed_missing: int = 0
    missing_check_skipped: bool = False


CATALOG_SPECS: "OrderedDict[str, CatalogSpec]" = OrderedDict(
    (
        ("rains", CatalogSpec("rains", "scripts/import_rains_products.py")),
        ("romerhus", CatalogSpec("romerhus", "scripts/import_romerhus_products.py")),
        ("suitclub", CatalogSpec("suitclub", "scripts/import_suitclub_products.py")),
        ("cejf", CatalogSpec("cejf", "scripts/import_cejf_products.py")),
        (
            "skagen_clothing",
            CatalogSpec("skagen_clothing", "scripts/import_skagen_clothing_products.py"),
        ),
        (
            "shoechapter",
            CatalogSpec("shoechapter", "scripts/import_shoechapter_products.py"),
        ),
        ("stoy", CatalogSpec("stoy", "scripts/import_stoy_products.py")),
        ("lakor", CatalogSpec("lakor", "scripts/import_lakor_products.py")),
    )
)


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


def parse_timestamp(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace(" ", "T").replace("Z", "+00:00")
    match = re.fullmatch(
        r"(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
        r"(?:\.(?P<fraction>\d+))?(?P<offset>[+-]\d{2}:\d{2})?",
        text,
    )
    if not match:
        raise ValueError(f"Invalid database timestamp: {value!r}")
    fraction = match.group("fraction")
    normalized = match.group("base")
    if fraction:
        normalized += "." + fraction[:6].ljust(6, "0")
    normalized += match.group("offset") or ""
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class CatalogLifecycleClient:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        *,
        snapshots_table: str,
        timeout_seconds: float = 60,
        max_retries: int = 2,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.snapshots_table = snapshots_table
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        headers = {"apikey": supabase_key, "Content-Type": "application/json"}
        if supabase_key.count(".") == 2:
            headers["Authorization"] = f"Bearer {supabase_key}"
        self.session.headers.update(headers)
        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "PATCH", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)

    def _table_url(self, table: str) -> str:
        return f"{self.supabase_url}/rest/v1/{quote(table)}"

    @staticmethod
    def _response_error(response: requests.Response, action: str) -> SupabasePatchError:
        body = (response.text or "").strip()
        if len(body) > 2000:
            body = body[:2000] + "..."
        return SupabasePatchError(
            f"Supabase {action} returned HTTP {response.status_code}: "
            f"{body or '<empty response body>'}"
        )

    def list_rows(self, store_key: str, page_size: int = 1000) -> list[dict[str, Any]]:
        spec = STORE_SPECS[store_key]
        columns = (
            "id",
            *spec.identity_columns,
            "source_url",
            "canonical_url",
            "current_price",
            "list_price",
            "webshop_sizes",
            "local_inventory",
            "local_available",
            "aarhus_available",
            "updated_at",
            "publication_status",
            "status_reason",
            "status_checked_at",
            "discontinued_at",
            "last_seen_in_catalog_at",
            "consecutive_catalog_misses",
        )
        if "local_total_stock" in spec.dynamic_columns:
            columns += ("local_total_stock",)
        if "aarhus_total_stock" in spec.dynamic_columns:
            columns += ("aarhus_total_stock",)

        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.session.get(
                self._table_url(spec.table),
                params={"select": ",".join(columns), "order": "id.asc"},
                headers={"Range": f"{start}-{start + page_size - 1}"},
                timeout=self.timeout_seconds,
            )
            if not response.ok:
                raise self._response_error(response, f"GET {spec.table}")
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            start += page_size

    def patch_ids(
        self,
        table: str,
        row_ids: list[int],
        payload: dict[str, Any],
        *,
        chunk_size: int = 100,
    ) -> None:
        for offset in range(0, len(row_ids), chunk_size):
            chunk = row_ids[offset : offset + chunk_size]
            response = self.session.patch(
                self._table_url(table),
                params={"id": f"in.({','.join(str(row_id) for row_id in chunk)})"},
                headers={"Prefer": "return=minimal"},
                data=json.dumps(payload, ensure_ascii=False),
                timeout=self.timeout_seconds,
            )
            if not response.ok:
                raise self._response_error(response, f"PATCH {table} ids={chunk[:3]}")

    def patch_row(self, table: str, row_id: int, payload: dict[str, Any]) -> None:
        self.patch_ids(table, [row_id], payload, chunk_size=1)

    def upsert_snapshots(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        response = self.session.post(
            self._table_url(self.snapshots_table),
            params={"on_conflict": "store,product_id,checked_bucket"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        if not response.ok:
            raise self._response_error(response, f"UPSERT {self.snapshots_table}")

    def close(self) -> None:
        self.session.close()


def importer_command(catalog: CatalogSpec, args: argparse.Namespace) -> list[str]:
    command = [sys.executable, str(ROOT / catalog.import_script), "--log-level", args.log_level]
    if args.dry_run:
        command.append("--dry-run")
    if args.no_delay:
        command.append("--no-delay")
    if args.limit:
        command.extend(("--limit", str(args.limit)))
    return command


def run_importer(catalog: CatalogSpec, args: argparse.Namespace) -> int:
    command = importer_command(catalog, args)
    LOG.info("Running weekly full import for %s: %s", catalog.key, " ".join(command))
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            timeout=args.import_timeout,
        )
    except subprocess.TimeoutExpired:
        LOG.error("%s full import exceeded the %.0f second timeout.", catalog.key, args.import_timeout)
        return 1
    return int(result.returncode)


def reconcile_catalog(
    store_key: str,
    rows_before: list[dict[str, Any]],
    rows_after: list[dict[str, Any]],
    import_started_at: datetime,
    checked_at: str,
    client: CatalogLifecycleClient,
    *,
    miss_confirmations: int,
    min_seen_ratio: float,
) -> CatalogStats:
    spec = STORE_SPECS[store_key]
    stats = CatalogStats(rows_before=len(rows_before), rows_after=len(rows_after))
    before_by_id = {int(row["id"]): row for row in rows_before}
    before_ids = set(before_by_id)
    baseline_ids = {
        row_id
        for row_id, row in before_by_id.items()
        if row.get("publication_status") != "missing"
    }
    seen_rows = [
        row
        for row in rows_after
        if parse_timestamp(row.get("updated_at")) >= import_started_at
    ]
    seen_ids = {int(row["id"]) for row in seen_rows}
    stats.seen = len(seen_rows)
    stats.new = len(seen_ids - before_ids)
    stats.reactivated = sum(
        1
        for row in seen_rows
        if row.get("publication_status") in {"missing", "unavailable"}
    )

    if seen_ids:
        client.patch_ids(
            spec.table,
            sorted(seen_ids),
            {
                "publication_status": "active",
                "status_reason": None,
                "status_checked_at": checked_at,
                "discontinued_at": None,
                "last_seen_in_catalog_at": checked_at,
                "consecutive_catalog_misses": 0,
            },
        )

    seen_baseline = len(seen_ids & baseline_ids)
    seen_ratio = seen_baseline / len(baseline_ids) if baseline_ids else 1.0
    if len(baseline_ids) >= 10 and seen_ratio < min_seen_ratio:
        stats.missing_check_skipped = True
        LOG.error(
            "%s safety check: only %s/%s previously publishable rows were refreshed "
            "(%.1f%% < %.1f%%). Seen rows were activated, but catalog misses were not advanced.",
            store_key,
            seen_baseline,
            len(baseline_ids),
            seen_ratio * 100,
            min_seen_ratio * 100,
        )
        return stats

    missing_snapshots: list[dict[str, Any]] = []
    for row in rows_after:
        row_id = int(row["id"])
        if row_id in seen_ids or row.get("publication_status") == "missing":
            continue
        misses = int(row.get("consecutive_catalog_misses") or 0) + 1
        if misses < miss_confirmations:
            client.patch_row(
                spec.table,
                row_id,
                {
                    "status_checked_at": checked_at,
                    "consecutive_catalog_misses": misses,
                },
            )
            stats.first_misses += 1
            continue

        dynamic = unavailable_payload(spec, row, checked_at)
        client.patch_row(
            spec.table,
            row_id,
            {
                **dynamic,
                "publication_status": "missing",
                "status_reason": f"not_seen_in_{miss_confirmations}_catalog_syncs",
                "status_checked_at": checked_at,
                "discontinued_at": row.get("discontinued_at") or checked_at,
                "consecutive_catalog_misses": misses,
            },
        )
        missing_snapshots.append(
            snapshot_payload(
                spec,
                row,
                dynamic,
                checked_at=checked_at,
                refresh_status="catalog_missing",
            )
        )
        stats.confirmed_missing += 1

    for offset in range(0, len(missing_snapshots), 50):
        client.upsert_snapshots(missing_snapshots[offset : offset + 50])
    return stats


def sync_catalogs(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required.")

    selected = (
        list(CATALOG_SPECS)
        if args.all or not args.store
        else list(OrderedDict.fromkeys(args.store))
    )
    client = (
        CatalogLifecycleClient(
            str(supabase_url),
            str(supabase_key),
            snapshots_table=args.snapshots_table,
            timeout_seconds=args.supabase_timeout,
            max_retries=args.max_retries,
        )
        if not args.dry_run
        else None
    )
    failed_stores = 0
    try:
        for store_key in selected:
            catalog = CATALOG_SPECS[store_key]
            rows_before = client.list_rows(store_key) if client is not None else []
            import_started_at = datetime.now(timezone.utc)
            return_code = run_importer(catalog, args)
            if return_code:
                failed_stores += 1
                LOG.error(
                    "%s import failed with exit code %s; lifecycle reconciliation was skipped.",
                    store_key,
                    return_code,
                )
                continue
            if args.dry_run or args.limit:
                LOG.info(
                    "%s import test complete; lifecycle reconciliation is disabled for dry-run or limited imports.",
                    store_key,
                )
                continue

            assert client is not None
            checked_at = datetime.now(timezone.utc).isoformat()
            rows_after = client.list_rows(store_key)
            try:
                stats = reconcile_catalog(
                    store_key,
                    rows_before,
                    rows_after,
                    import_started_at,
                    checked_at,
                    client,
                    miss_confirmations=args.miss_confirmations,
                    min_seen_ratio=args.min_seen_ratio,
                )
            except SupabasePatchError:
                raise
            LOG.info(
                "%s catalog sync complete. before=%s after=%s seen=%s new=%s "
                "reactivated=%s first_misses=%s confirmed_missing=%s miss_check_skipped=%s",
                store_key,
                stats.rows_before,
                stats.rows_after,
                stats.seen,
                stats.new,
                stats.reactivated,
                stats.first_misses,
                stats.confirmed_missing,
                stats.missing_check_skipped,
            )
            if stats.missing_check_skipped:
                failed_stores += 1
    finally:
        if client is not None:
            client.close()
    LOG.info("Weekly catalog sync complete. stores=%s failed_stores=%s", len(selected), failed_stores)
    return 1 if failed_stores else 0


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Run existing non-Kaufmann full imports sequentially, add new products, "
            "and reconcile weekly catalog lifecycle state."
        )
    )
    parser.add_argument(
        "--store",
        action="append",
        choices=list(CATALOG_SPECS),
        help="Sync one store; repeat for an explicit sequence. Defaults to all stores.",
    )
    parser.add_argument("--all", action="store_true", help="Sync every configured store sequentially.")
    parser.add_argument("--dry-run", action="store_true", help="Run importers without database writes or lifecycle reconciliation.")
    parser.add_argument("--limit", type=int, help="Limit each importer for testing; disables lifecycle reconciliation.")
    parser.add_argument("--no-delay", action="store_true", help="Disable polite importer delays for bounded tests only.")
    parser.add_argument(
        "--miss-confirmations",
        type=int,
        default=int(env("CATALOG_MISS_CONFIRMATIONS", "2")),
        help="Successful weekly imports a row must miss before it becomes missing.",
    )
    parser.add_argument(
        "--min-seen-ratio",
        type=float,
        default=float(env("CATALOG_MIN_SEEN_RATIO", "0.5")),
        help="Minimum fraction of existing publishable rows that a full import must refresh before misses advance.",
    )
    parser.add_argument(
        "--import-timeout",
        type=float,
        default=float(env("CATALOG_IMPORT_TIMEOUT", "43200")),
        help="Per-store full-import timeout in seconds.",
    )
    parser.add_argument(
        "--snapshots-table",
        default=env("STORE_INVENTORY_SNAPSHOTS_TABLE", "store_inventory_snapshots"),
    )
    parser.add_argument("--supabase-timeout", type=float, default=float(env("STORE_REFRESH_SUPABASE_TIMEOUT", "60")))
    parser.add_argument("--max-retries", type=int, default=int(env("STORE_REFRESH_MAX_RETRIES", "2")))
    parser.add_argument(
        "--log-level",
        default=env("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    arguments = parser.parse_args()
    if arguments.all and arguments.store:
        parser.error("use either --all or one or more --store values")
    if arguments.limit is not None and arguments.limit < 1:
        parser.error("--limit must be at least 1")
    if arguments.miss_confirmations < 2:
        parser.error("--miss-confirmations must be at least 2")
    if not 0 < arguments.min_seen_ratio <= 1:
        parser.error("--min-seen-ratio must satisfy 0 < value <= 1")
    if arguments.import_timeout <= 0 or arguments.supabase_timeout <= 0:
        parser.error("timeout values must be greater than 0")
    if arguments.max_retries < 0:
        parser.error("--max-retries cannot be negative")
    return arguments


if __name__ == "__main__":
    cli_args = parse_args()
    logging.basicConfig(
        level=getattr(logging, cli_args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(sync_catalogs(cli_args))
