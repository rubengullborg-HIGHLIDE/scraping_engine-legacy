from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CatalogSyncRunRecorder:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        *,
        table: str = "catalog_sync_runs",
        timeout_seconds: float = 60,
        max_retries: int = 2,
    ) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.table = table
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
            allowed_methods=frozenset({"POST", "PATCH"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    @property
    def table_url(self) -> str:
        return f"{self.supabase_url}/rest/v1/{quote(self.table)}"

    @staticmethod
    def _raise(response: requests.Response, action: str) -> None:
        if response.ok:
            return
        body = (response.text or "").strip()
        raise RuntimeError(
            f"Supabase {action} returned HTTP {response.status_code}: "
            f"{body[:2000] or '<empty response body>'}"
        )

    def start(
        self,
        *,
        batch_id: str,
        store: str,
        run_type: str,
        started_at: str,
        rows_before: int,
        details: Optional[dict[str, Any]] = None,
    ) -> int:
        payload = {
            "batch_id": batch_id,
            "store": store,
            "run_type": run_type,
            "status": "running",
            "started_at": started_at,
            "rows_before": rows_before,
            "details": details or {},
        }
        response = self.session.post(
            self.table_url,
            headers={"Prefer": "return=representation"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        self._raise(response, f"INSERT {self.table}")
        rows = response.json()
        if not rows or "id" not in rows[0]:
            raise RuntimeError(f"Supabase INSERT {self.table} returned no run id")
        return int(rows[0]["id"])

    def finish(self, run_id: int, **fields: Any) -> None:
        payload = {**fields, "updated_at": utc_now()}
        response = self.session.patch(
            self.table_url,
            params={"id": f"eq.{run_id}"},
            headers={"Prefer": "return=minimal"},
            data=json.dumps(payload, ensure_ascii=False),
            timeout=self.timeout_seconds,
        )
        self._raise(response, f"PATCH {self.table} id={run_id}")

    def fail(
        self,
        run_id: int,
        message: str,
        *,
        rows_after: int = 0,
        failed_products: int = 0,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": "failed",
            "completed_at": utc_now(),
            "rows_after": rows_after,
            "failed_products": failed_products,
            "error_message": message[:2000],
        }
        if details is not None:
            payload["details"] = details
        self.finish(run_id, **payload)

    def close(self) -> None:
        self.session.close()
