import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import special_store_batches as special
from scripts import refresh_store_inventory as stores
from scripts.sync_store_catalogs import CatalogLifecycleClient


def mock_client(cls):
    client = object.__new__(cls)
    client.supabase_url = "https://example.supabase.co"
    client.timeout_seconds = 1
    client.session = Mock()
    client.session.get.return_value.json.return_value = []
    return client


class DailyAarhusSelectionTests(unittest.TestCase):
    def test_every_store_daily_query_requires_active_and_aarhus_available(self):
        client = mock_client(stores.SupabaseInventoryClient)
        for spec in stores.STORE_SPECS.values():
            with self.subTest(store=spec.key):
                client.list_rows(spec)
                params = client.session.get.call_args.kwargs["params"]
                self.assertEqual(params["publication_status"], "eq.active")
                self.assertEqual(params["aarhus_available"], "eq.true")

    def test_kaufmann_weekly_sweep_still_includes_sold_out_and_unpublished(self):
        client = mock_client(stores.SupabaseInventoryClient)
        special.read_rows(client, "kaufmann", include_unavailable=True)
        params = client.session.get.call_args.kwargs["params"]
        self.assertNotIn("aarhus_available", params)
        self.assertNotIn("publication_status", params)
        self.assertIn("consecutive_source_misses", params["select"])

    def test_weekly_catalogue_selection_remains_unfiltered_for_every_store(self):
        client = mock_client(CatalogLifecycleClient)
        for store in stores.STORE_SPECS:
            with self.subTest(store=store):
                client.list_rows(store)
                params = client.session.get.call_args.kwargs["params"]
                self.assertNotIn("aarhus_available", params)
                self.assertNotIn("publication_status", params)

    def test_local_samples_exclude_sold_out_unknown_and_unpublished(self):
        rows = [
            {"id": 1, "publication_status": "active", "aarhus_available": True},
            {"id": 2, "publication_status": "active", "aarhus_available": False},
            {"id": 3, "publication_status": "active", "aarhus_available": None},
            {"id": 4, "publication_status": "unavailable", "aarhus_available": True},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rows.json"
            path.write_text(json.dumps(rows))
            client = stores.LocalInventoryClient(path)
            self.assertEqual(client.list_rows(stores.STORE_SPECS["salling"]), rows[:1])


if __name__ == "__main__":
    unittest.main()
