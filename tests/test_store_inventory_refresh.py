from __future__ import annotations

import unittest
from argparse import Namespace

from scripts.refresh_store_inventory import (
    RefreshStats,
    STORE_SPECS,
    apply_full_row,
    apply_unavailable,
    dynamic_payload,
    ensure_identity,
    snapshot_payload,
    unavailable_payload,
    zero_inventory,
)


CHECKED_AT = "2026-08-26T10:00:00+00:00"


class StoreInventoryRefreshTests(unittest.TestCase):
    def test_zero_inventory_preserves_store_shape_and_stock_certainty(self) -> None:
        source = {
            "stores": {
                "exact": {
                    "name": "Exact Store",
                    "stock_known": True,
                    "available": True,
                    "total_stock": 3,
                    "sizes": {"M": {"available": True, "stock": 3}},
                },
                "boolean": {
                    "name": "Boolean Store",
                    "stock_known": False,
                    "available": True,
                    "total_stock": None,
                    "sizes": {"L": {"available": True, "stock": None}},
                },
            }
        }

        result = zero_inventory(source)

        self.assertFalse(result["stores"]["exact"]["available"])
        self.assertEqual(0, result["stores"]["exact"]["total_stock"])
        self.assertEqual(0, result["stores"]["exact"]["sizes"]["M"]["stock"])
        self.assertFalse(result["stores"]["boolean"]["available"])
        self.assertIsNone(result["stores"]["boolean"]["total_stock"])
        self.assertIsNone(result["stores"]["boolean"]["sizes"]["L"]["stock"])
        self.assertTrue(source["stores"]["exact"]["available"])

    def test_unavailable_payload_matches_each_table_dynamic_contract(self) -> None:
        existing = {
            "current_price": 500,
            "list_price": 700,
            "local_inventory": {"stores": {}},
        }
        for spec in STORE_SPECS.values():
            with self.subTest(store=spec.key):
                payload = unavailable_payload(spec, existing, CHECKED_AT)
                self.assertEqual(set(spec.dynamic_columns), set(payload))
                self.assertFalse(payload["local_available"])
                self.assertFalse(payload["aarhus_available"])
                self.assertEqual(500, payload["current_price"])

        cejf = unavailable_payload(STORE_SPECS["cejf"], existing, CHECKED_AT)
        self.assertIsNone(cejf["local_total_stock"])
        self.assertIsNone(cejf["aarhus_total_stock"])
        rains = unavailable_payload(STORE_SPECS["rains"], existing, CHECKED_AT)
        self.assertEqual(0, rains["local_total_stock"])
        self.assertEqual(0, rains["aarhus_total_stock"])

    def test_dynamic_payload_rejects_incomplete_parser_output(self) -> None:
        with self.assertRaisesRegex(ValueError, "omitted required columns"):
            dynamic_payload(STORE_SPECS["rains"], {"current_price": 500})

    def test_identity_guard_uses_stable_source_product_id(self) -> None:
        ensure_identity(
            STORE_SPECS["cejf"],
            {"id": 1, "source_product_id": "123"},
            {"source_product_id": "123"},
        )
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            ensure_identity(
                STORE_SPECS["cejf"],
                {"id": 1, "source_product_id": "123"},
                {"source_product_id": "456"},
            )

    def test_snapshot_payload_uses_utc_day_and_unified_source_item_id(self) -> None:
        payload = snapshot_payload(
            STORE_SPECS["cejf"],
            {
                "id": 17,
                "source_product_id": "987654",
                "canonical_url": "https://cejf.dk/products/example",
                "source_url": "https://cejf.dk/products/example",
            },
            {
                "current_price": 699,
                "list_price": 899,
                "webshop_sizes": [{"size": "M", "in_stock": True}],
                "local_inventory": {"stores": {}},
                "local_total_stock": None,
                "local_available": True,
                "aarhus_total_stock": None,
                "aarhus_available": True,
            },
            checked_at="2026-08-26T00:30:00+02:00",
            refresh_status="ok",
        )

        self.assertEqual("cejf", payload["store"])
        self.assertEqual(17, payload["product_id"])
        self.assertEqual("987654", payload["source_item_id"])
        self.assertEqual("2026-08-25", payload["checked_bucket"])
        self.assertEqual(699, payload["current_price"])
        self.assertTrue(payload["aarhus_available"])

    def test_live_patch_is_followed_by_snapshot_queue(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.events: list[tuple[str, object]] = []

            def patch_row(self, spec, row_id, payload) -> None:
                self.events.append(("patch", row_id))

            def queue_snapshot(self, payload) -> None:
                self.events.append(("snapshot", payload))

        spec = STORE_SPECS["cejf"]
        checked_at = "2026-08-26T10:00:00+00:00"
        full_row = {
            "source_product_id": "123",
            "current_price": 699,
            "list_price": None,
            "webshop_sizes": [],
            "local_inventory": {"stores": {}},
            "local_total_stock": None,
            "local_available": False,
            "aarhus_total_stock": None,
            "aarhus_available": False,
            "inventory_checked_at": checked_at,
            "scraped_at": checked_at,
            "updated_at": checked_at,
        }
        database = FakeDatabase()
        stats = RefreshStats()

        apply_full_row(
            spec,
            database,  # type: ignore[arg-type]
            {
                "id": 4,
                "source_product_id": "123",
                "canonical_url": "https://cejf.dk/products/example",
                "source_url": "https://cejf.dk/products/example",
            },
            full_row,
            Namespace(dry_run=False, no_snapshots=False),
            stats,
        )

        self.assertEqual(["patch", "snapshot"], [event[0] for event in database.events])
        self.assertEqual("2026-08-26", database.events[1][1]["checked_bucket"])
        self.assertEqual(1, stats.updated)
        self.assertEqual(1, stats.snapshots)

    def test_explicitly_unavailable_row_records_status_snapshot(self) -> None:
        class FakeDatabase:
            def __init__(self) -> None:
                self.snapshots: list[dict[str, object]] = []

            def patch_row(self, spec, row_id, payload) -> None:
                self.payload = payload

            def queue_snapshot(self, payload) -> None:
                self.snapshots.append(payload)

        database = FakeDatabase()
        stats = RefreshStats()
        apply_unavailable(
            STORE_SPECS["rains"],
            database,  # type: ignore[arg-type]
            {
                "id": 9,
                "source_parent_id": "19030",
                "source_color_id": "19030-177",
                "canonical_url": "https://www.dk.rains.com/products/example",
                "source_url": "https://www.dk.rains.com/products/example",
                "current_price": 999,
                "list_price": None,
                "local_inventory": {"stores": {}},
            },
            Namespace(dry_run=False, no_snapshots=False),
            stats,
            refresh_status="source_item_missing",
        )

        self.assertEqual(1, len(database.snapshots))
        self.assertEqual("source_item_missing", database.snapshots[0]["refresh_status"])
        self.assertFalse(database.snapshots[0]["aarhus_available"])
        self.assertEqual(1, stats.unavailable)
        self.assertEqual(1, stats.snapshots)


if __name__ == "__main__":
    unittest.main()
