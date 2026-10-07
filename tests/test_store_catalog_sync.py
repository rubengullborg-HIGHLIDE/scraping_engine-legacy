from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts.sync_store_catalogs import parse_timestamp, reconcile_catalog


STARTED_AT = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
CHECKED_AT = "2026-08-26T11:00:00+00:00"


def rains_row(
    row_id: int,
    *,
    updated_at: str,
    publication_status: str = "active",
    misses: int = 0,
) -> dict:
    return {
        "id": row_id,
        "source_parent_id": f"style-{row_id}",
        "source_color_id": f"color-{row_id}",
        "source_url": f"https://www.dk.rains.com/products/item-{row_id}",
        "canonical_url": f"https://www.dk.rains.com/products/item-{row_id}",
        "current_price": 999,
        "list_price": None,
        "webshop_sizes": [{"size": "M", "in_stock": True}],
        "local_inventory": {
            "stores": {
                "rains-aarhus": {
                    "name": "Rains Aarhus",
                    "stock_known": True,
                    "available": True,
                    "total_stock": 2,
                    "sizes": {"M": {"available": True, "stock": 2}},
                }
            }
        },
        "local_total_stock": 2,
        "local_available": True,
        "aarhus_total_stock": 2,
        "aarhus_available": True,
        "updated_at": updated_at,
        "publication_status": publication_status,
        "status_reason": None,
        "status_checked_at": None,
        "discontinued_at": None,
        "last_seen_in_catalog_at": None,
        "consecutive_catalog_misses": misses,
    }


class FakeLifecycleClient:
    def __init__(self) -> None:
        self.bulk_patches: list[tuple[str, list[int], dict]] = []
        self.row_patches: list[tuple[str, int, dict]] = []
        self.snapshots: list[dict] = []

    def patch_ids(self, table: str, row_ids: list[int], payload: dict, **kwargs) -> None:
        self.bulk_patches.append((table, row_ids, payload))

    def patch_row(self, table: str, row_id: int, payload: dict) -> None:
        self.row_patches.append((table, row_id, payload))

    def insert_history(self, rows: list[dict]) -> None:
        self.snapshots.extend(rows)


class StoreCatalogSyncTests(unittest.TestCase):
    def test_supabase_variable_precision_timestamp_parses_on_python_39(self) -> None:
        parsed = parse_timestamp("2026-08-26T10:35:45.39733+00:00")

        self.assertEqual(397330, parsed.microsecond)
        self.assertEqual(timezone.utc, parsed.tzinfo)

    def test_seen_rows_reactivate_new_rows_and_first_miss_is_not_hidden(self) -> None:
        before = [
            rains_row(1, updated_at="2026-08-20T10:00:00+00:00", publication_status="unavailable"),
            rains_row(2, updated_at="2026-08-20T10:00:00+00:00"),
        ]
        after = [
            rains_row(1, updated_at="2026-08-26T10:30:00+00:00", publication_status="unavailable"),
            rains_row(2, updated_at="2026-08-20T10:00:00+00:00"),
            rains_row(3, updated_at="2026-08-26T10:31:00+00:00"),
        ]
        client = FakeLifecycleClient()

        stats = reconcile_catalog(
            "rains",
            before,
            after,
            STARTED_AT,
            CHECKED_AT,
            client,  # type: ignore[arg-type]
            miss_confirmations=2,
            min_seen_ratio=0.5,
        )

        self.assertEqual(2, stats.seen)
        self.assertEqual(1, stats.new)
        self.assertEqual(1, stats.reactivated)
        self.assertEqual(1, stats.first_misses)
        self.assertEqual([1, 3], client.bulk_patches[0][1])
        self.assertEqual("active", client.bulk_patches[0][2]["publication_status"])
        self.assertEqual(2, client.row_patches[0][1])
        self.assertEqual(1, client.row_patches[0][2]["consecutive_catalog_misses"])
        self.assertFalse(client.snapshots)

    def test_second_catalog_miss_tombstones_inventory_and_writes_snapshot(self) -> None:
        row = rains_row(
            7,
            updated_at="2026-08-20T10:00:00+00:00",
            misses=1,
        )
        client = FakeLifecycleClient()

        stats = reconcile_catalog(
            "rains",
            [row],
            [row],
            STARTED_AT,
            CHECKED_AT,
            client,  # type: ignore[arg-type]
            miss_confirmations=2,
            min_seen_ratio=0.5,
        )

        self.assertEqual(1, stats.confirmed_missing)
        patch = client.row_patches[0][2]
        self.assertEqual("missing", patch["publication_status"])
        self.assertFalse(patch["aarhus_available"])
        self.assertEqual(0, patch["aarhus_total_stock"])
        self.assertEqual([], patch["webshop_sizes"])
        self.assertEqual("catalog_missing", client.snapshots[0]["refresh_status"])

    def test_large_catalog_drop_skips_advancing_misses(self) -> None:
        before = [
            rains_row(row_id, updated_at="2026-08-20T10:00:00+00:00")
            for row_id in range(1, 11)
        ]
        after = [dict(row) for row in before]
        after[0]["updated_at"] = "2026-08-26T10:30:00+00:00"
        client = FakeLifecycleClient()

        stats = reconcile_catalog(
            "rains",
            before,
            after,
            STARTED_AT,
            CHECKED_AT,
            client,  # type: ignore[arg-type]
            miss_confirmations=2,
            min_seen_ratio=0.5,
        )

        self.assertTrue(stats.missing_check_skipped)
        self.assertEqual([1], client.bulk_patches[0][1])
        self.assertFalse(client.row_patches)
        self.assertFalse(client.snapshots)


if __name__ == "__main__":
    unittest.main()


class AxelQuintLifecycleTests(unittest.TestCase):
    def test_sunday_retains_explicit_unavailable_and_reactivates_returning_colour(self):
        for store in ('axel', 'quint'):
            before = [rains_row(i, updated_at='2026-08-25T10:00:00Z', publication_status='unavailable') for i in (1, 2)]
            after = [rains_row(1, updated_at=CHECKED_AT, publication_status='unavailable'),
                     rains_row(2, updated_at=CHECKED_AT)]
            after[0]['status_reason'] = 'source_available_false'
            client = FakeLifecycleClient()
            stats = reconcile_catalog(store, before, after, STARTED_AT, CHECKED_AT, client,
                                      miss_confirmations=2, min_seen_ratio=.5)
            self.assertEqual(stats.reactivated, 1)
            self.assertEqual(stats.seen, 2)
            active = [ids for _, ids, payload in client.bulk_patches if payload.get('publication_status') == 'active']
            self.assertEqual(active, [[2]])
            self.assertTrue(any(ids == [1] and payload.get('consecutive_catalog_misses') == 0
                                for _, ids, payload in client.bulk_patches))
