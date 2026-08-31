from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
import uuid
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

from scrapers.full_import.kaufmann import KaufmanScraper  # noqa: E402
from scripts.catalog_sync_tracking import CatalogSyncRunRecorder, utc_now  # noqa: E402


LOG = logging.getLogger("sync_kaufmann_catalog")


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


class KaufmannCatalogClient:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        *,
        timeout_seconds: float = 60,
        max_retries: int = 2,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
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
            allowed_methods=frozenset({"GET", "POST"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def table_url(self, table: str) -> str:
        return f"{self.supabase_url}/rest/v1/{quote(table)}"

    @staticmethod
    def _raise(response: requests.Response, action: str) -> None:
        if response.ok:
            return
        raise RuntimeError(
            f"Supabase {action} returned HTTP {response.status_code}: "
            f"{(response.text or '')[:2000]}"
        )

    def list_rows(self, table: str, page_size: int = 1000) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        while True:
            response = self.session.get(
                self.table_url(table),
                params={"select": "id,canonical_url,source_url", "order": "id.asc"},
                headers={"Range": f"{start}-{start + page_size - 1}"},
                timeout=self.timeout_seconds,
            )
            self._raise(response, f"GET {table}")
            batch = response.json()
            rows.extend(batch)
            if len(batch) < page_size:
                return rows
            start += page_size

    def upsert_products(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        response = self.session.post(
            self.table_url(table),
            params={"on_conflict": "source_parent_id,source_color_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            data=json.dumps(rows, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        self._raise(response, f"UPSERT {table}")

    def close(self) -> None:
        self.session.close()


def normalized_known_urls(
    scraper: KaufmanScraper, rows: list[dict[str, Any]]
) -> set[str]:
    return {
        scraper._clean_product_url(str(row.get("canonical_url") or row.get("source_url")))
        for row in rows
        if row.get("canonical_url") or row.get("source_url")
    }


def candidate_urls_from_file(
    scraper: KaufmanScraper, path: Optional[str]
) -> set[str]:
    if not path:
        return set()
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return set()
    values = json.loads(text)
    if not isinstance(values, list):
        raise ValueError("Kaufmann candidate file must contain a JSON array")
    return {
        scraper._clean_product_url(str(value))
        for value in values
        if str(value).strip()
    }


def sync_kaufmann_catalog(args: argparse.Namespace) -> int:
    load_dotenv(ROOT / ".env")
    supabase_url = env("SUPABASE_URL")
    supabase_key = env("SUPABASE_SECRET_KEY") or env("SUPABASE_SERVICE_ROLE_KEY")
    if not args.discover_only and (not supabase_url or not supabase_key):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required.")

    scraper = KaufmanScraper(launch_browser=False)
    client: Optional[KaufmannCatalogClient] = None
    recorder: Optional[CatalogSyncRunRecorder] = None
    run_id: Optional[int] = None
    started_at = datetime.now(timezone.utc)
    try:
        sitemap_urls = set(
            scraper.discover_product_links_from_sitemap(args.sitemap_url)
        )
        LOG.info("Discovered %s Kaufmann product URLs in the sitemap.", len(sitemap_urls))
        if args.discover_only:
            for url in sorted(sitemap_urls)[: args.preview]:
                LOG.info("Discovered URL: %s", url)
            return 0 if sitemap_urls else 1

        assert supabase_url is not None and supabase_key is not None
        client = KaufmannCatalogClient(
            supabase_url,
            supabase_key,
            timeout_seconds=args.supabase_timeout,
            max_retries=args.max_retries,
        )
        rows_before = client.list_rows(args.products_table)
        known_urls = normalized_known_urls(scraper, rows_before)
        new_urls = sorted(sitemap_urls - known_urls)
        absent_urls = sorted(known_urls - sitemap_urls)
        unknown_color_urls = candidate_urls_from_file(
            scraper, args.new_variant_candidates
        )

        if len(known_urls) >= 10 and len(sitemap_urls) < len(known_urls) * args.min_seen_ratio:
            message = (
                f"Sitemap safety check failed: found {len(sitemap_urls)} URLs for "
                f"{len(known_urls)} known pages"
            )
            LOG.error(message)
            if not args.dry_run and not args.limit:
                recorder = CatalogSyncRunRecorder(
                    supabase_url,
                    supabase_key,
                    table=args.sync_runs_table,
                    timeout_seconds=args.supabase_timeout,
                    max_retries=args.max_retries,
                )
                run_id = recorder.start(
                    batch_id=str(uuid.uuid4()),
                    store="kaufmann",
                    run_type="kaufmann_catalog_discovery",
                    started_at=started_at.isoformat(),
                    rows_before=len(rows_before),
                    details={"known_pages": len(known_urls)},
                )
                recorder.fail(
                    run_id,
                    message,
                    rows_after=len(rows_before),
                    details={
                        "known_pages": len(known_urls),
                        "sitemap_pages": len(sitemap_urls),
                        "safety_blocked": True,
                    },
                )
            return 1

        import_urls = sorted(set(new_urls) | unknown_color_urls)
        selected_urls = import_urls[: args.limit] if args.limit else import_urls
        LOG.info(
            "Kaufmann catalogue diff: known_pages=%s new_pages=%s absent_pages=%s "
            "unknown_colour_pages=%s selected_import_pages=%s",
            len(known_urls),
            len(new_urls),
            len(absent_urls),
            len(unknown_color_urls),
            len(selected_urls),
        )
        if args.dry_run:
            for url in selected_urls:
                LOG.info("Dry run Kaufmann catalogue import URL: %s", url)
            return 0

        if not args.limit:
            recorder = CatalogSyncRunRecorder(
                supabase_url,
                supabase_key,
                table=args.sync_runs_table,
                timeout_seconds=args.supabase_timeout,
                max_retries=args.max_retries,
            )
            run_id = recorder.start(
                batch_id=str(uuid.uuid4()),
                store="kaufmann",
                run_type="kaufmann_catalog_discovery",
                started_at=started_at.isoformat(),
                rows_before=len(rows_before),
                details={"known_pages": len(known_urls)},
            )

        imported_variants = 0
        failed_pages = 0
        for index, product_url in enumerate(selected_urls, start=1):
            if index > 1 and not args.no_delay:
                time.sleep(random.uniform(args.min_delay, args.max_delay))
            try:
                LOG.info(
                    "[%s/%s] Importing Kaufmann catalogue candidate page %s",
                    index,
                    len(selected_urls),
                    product_url,
                )
                rows = scraper.parse_product_variants_with_js(
                    product_url, allow_unavailable=True
                )
                if not rows:
                    raise RuntimeError("catalogue candidate page returned no product variants")
                client.upsert_products(args.products_table, rows)
                imported_variants += len(rows)
            except Exception:
                failed_pages += 1
                LOG.exception("Failed to import Kaufmann catalogue candidate %s", product_url)

        rows_after = client.list_rows(args.products_table)
        new_product_rows = max(0, len(rows_after) - len(rows_before))
        if recorder is not None and run_id is not None:
            recorder.finish(
                run_id,
                status="success" if failed_pages == 0 else "partial",
                completed_at=utc_now(),
                rows_after=len(rows_after),
                products_seen=len(sitemap_urls),
                new_products=new_product_rows,
                absent_products=len(absent_urls),
                failed_products=failed_pages,
                error_message=(
                    None
                    if failed_pages == 0
                    else f"{failed_pages} newly discovered Kaufmann pages failed to import"
                ),
                details={
                    "known_pages": len(known_urls),
                    "sitemap_pages": len(sitemap_urls),
                    "new_pages": len(new_urls),
                    "unknown_colour_pages": len(unknown_color_urls),
                    "imported_variants": imported_variants,
                    "absent_pages": len(absent_urls),
                },
            )
        LOG.info(
            "Kaufmann catalogue discovery complete. new_pages=%s "
            "unknown_colour_pages=%s new_variant_rows=%s absent_pages=%s failed_pages=%s",
            len(new_urls),
            len(unknown_color_urls),
            new_product_rows,
            len(absent_urls),
            failed_pages,
        )
        return 1 if failed_pages else 0
    except Exception as exc:
        if recorder is not None and run_id is not None:
            try:
                recorder.fail(run_id, str(exc))
            except Exception:
                LOG.exception("Failed to record the Kaufmann catalogue run failure")
        raise
    finally:
        if recorder is not None:
            recorder.close()
        if client is not None:
            client.close()
        scraper.close()


def parse_args() -> argparse.Namespace:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Compare Kaufmann's sitemap with known product pages and full-import only "
            "newly discovered pages."
        )
    )
    parser.add_argument(
        "--products-table",
        default=env("KAUFMANN_PRODUCTS_TABLE", "kaufmann_products"),
    )
    parser.add_argument(
        "--sync-runs-table",
        default=env("CATALOG_SYNC_RUNS_TABLE", "catalog_sync_runs"),
    )
    parser.add_argument(
        "--sitemap-url",
        default=env("KAUFMANN_SITEMAP_URL", "https://www.kaufmann.dk/sitemap.xml"),
    )
    parser.add_argument(
        "--new-variant-candidates",
        help=(
            "JSON file written by the weekly inventory sweep containing known "
            "product pages with unknown colour IDs."
        ),
    )
    parser.add_argument("--discover-only", action="store_true")
    parser.add_argument("--preview", type=int, default=10)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-delay", action="store_true")
    parser.add_argument(
        "--min-delay", type=float, default=float(env("FULL_IMPORT_MIN_DELAY", "1.5"))
    )
    parser.add_argument(
        "--max-delay", type=float, default=float(env("FULL_IMPORT_MAX_DELAY", "3.0"))
    )
    parser.add_argument(
        "--min-seen-ratio",
        type=float,
        default=float(env("CATALOG_MIN_SEEN_RATIO", "0.5")),
    )
    parser.add_argument(
        "--supabase-timeout",
        type=float,
        default=float(env("STORE_REFRESH_SUPABASE_TIMEOUT", "60")),
    )
    parser.add_argument(
        "--max-retries", type=int, default=int(env("STORE_REFRESH_MAX_RETRIES", "2"))
    )
    parser.add_argument(
        "--log-level",
        default=env("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not 0 < args.min_seen_ratio <= 1:
        parser.error("--min-seen-ratio must satisfy 0 < value <= 1")
    if args.max_delay < args.min_delay:
        parser.error("--max-delay must be greater than or equal to --min-delay")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    logging.basicConfig(
        level=getattr(logging, arguments.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(sync_kaufmann_catalog(arguments))
