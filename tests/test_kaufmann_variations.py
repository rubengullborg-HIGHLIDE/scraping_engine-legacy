from __future__ import annotations

import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from scrapers.stores.kaufmann_variations import (
    KaufmannVariationClient,
    KaufmannVariationUnavailable,
    KaufmannVariationParseError,
    clean_product_url,
    extract_source_parent_id,
    parse_price,
    variation_rows_from_payload,
)
from scripts import refresh_kaufmann_inventory as kaufmann
from scripts.refresh_store_inventory import parse_args
from scripts.run_inventory_batches import StagedClient


CHECKED_AT = "2026-08-26T08:00:00+00:00"


def variation_payload(*, source_available: bool, webshop_stock: int, aarhus_stock: int) -> dict:
    return {
        "totalOptions": 1 if source_available else 0,
        "options": {
            "color": {
                "color-1": {
                    "available": source_available,
                    "name": "NAVY",
                    "colorGroup": "Blå",
                    "price": "DKK 299",
                    "priceRaw": 299,
                    "listPrice": "DKK 400",
                    "listPriceRaw": None,
                    "sizes": {
                        "size-1": {
                            "sizeName": "M",
                            "availability": webshop_stock,
                            "productId": "size-1",
                            "productNumber": "1234560048",
                            "stock": {
                                "warehouse-online": {
                                    "seoUrl": "online-shop",
                                    "available": webshop_stock > 0,
                                    "stock": webshop_stock,
                                },
                                "warehouse-aarhus": {
                                    "seoUrl": "bruuns-galleri",
                                    "available": aarhus_stock > 0,
                                    "stock": aarhus_stock,
                                },
                            },
                        }
                    },
                }
            }
        },
    }


def product_row(**overrides: object) -> dict:
    row = {
        "id": 785,
        "source_parent_id": "parent-1",
        "source_color_id": "color-1",
        "source_url": "https://www.kaufmann.dk/produkt/example?color=color-1",
        "canonical_url": "https://www.kaufmann.dk/produkt/example",
        "publication_status": "active",
        "source_available": True,
        "status_reason": None,
        "discontinued_at": None,
        "consecutive_source_misses": 0,
    }
    row.update(overrides)
    return row


class KaufmannVariationParserTests(unittest.TestCase):
    def test_empty_variation_requires_page_confirmation_before_unavailable(self):
        client = KaufmannVariationClient(max_retries=0)
        self.addCleanup(client.close)
        parent = 'a' * 32
        payload = {'status': 200, 'hasVariants': False, 'totalOptions': 0, 'options': {'color': []}}
        response = Mock(status_code=200)
        response.json.return_value = payload
        url = 'https://www.kaufmann.dk/produkt/example'
        for status in (404, 410):
            with patch.object(client.session, 'get', side_effect=[response, Mock(status_code=status)]):
                with self.assertRaises(KaufmannVariationUnavailable) as caught:
                    client.fetch_payload(parent, product_url=url)
            self.assertEqual(caught.exception.url, url)
        live_page = Mock(status_code=200, text=f"productStore.setup('{parent}')")
        with patch.object(client.session, 'get', side_effect=[response, live_page]):
            current = client.fetch_payload(parent, product_url=url)
        with self.assertRaises(KaufmannVariationParseError):
            variation_rows_from_payload(current, parent, url)

    def test_active_color_maps_prices_and_exact_aarhus_stock(self) -> None:
        rows = variation_rows_from_payload(
            variation_payload(
                source_available=True,
                webshop_stock=4,
                aarhus_stock=3,
            ),
            "parent-1",
            "https://www.kaufmann.dk/produkt/example",
            checked_at=CHECKED_AT,
        )

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(299.0, row["current_price"])
        self.assertEqual(400.0, row["list_price"])
        self.assertTrue(row["source_available"])
        self.assertEqual("active", row["publication_status"])
        self.assertTrue(row["webshop_sizes"][0]["in_stock"])
        self.assertEqual(4, row["webshop_sizes"][0]["stock"])
        self.assertEqual(3, row["aarhus_total_stock"])
        self.assertTrue(row["aarhus_available"])
        self.assertEqual(3, row["local_total_stock"])
        self.assertTrue(row["local_available"])
        self.assertEqual(CHECKED_AT, row["inventory_checked_at"])
        self.assertNotIn("aarhus_inventory", row)
        self.assertEqual(
            3,
            row["local_inventory"]["stores"]["bruuns-galleri"]["sizes"]["M"][
                "stock"
            ],
        )
        self.assertIsNone(row["discontinued_at"])

    def test_source_unavailable_zeroes_residual_clean_stock(self) -> None:
        row = variation_rows_from_payload(
            variation_payload(
                source_available=False,
                webshop_stock=5,
                aarhus_stock=2,
            ),
            "parent-1",
            "https://www.kaufmann.dk/produkt/example",
            checked_at=CHECKED_AT,
        )[0]

        self.assertFalse(row["source_available"])
        self.assertEqual("unavailable", row["publication_status"])
        self.assertEqual("source_available_false", row["status_reason"])
        self.assertFalse(row["webshop_sizes"][0]["in_stock"])
        self.assertEqual(0, row["webshop_sizes"][0]["stock"])
        self.assertEqual(0, row["aarhus_total_stock"])
        self.assertFalse(row["aarhus_available"])
        self.assertFalse(
            row["local_inventory"]["stores"]["bruuns-galleri"]["available"]
        )
        self.assertEqual(
            0,
            row["local_inventory"]["stores"]["bruuns-galleri"]["sizes"]["M"][
                "stock"
            ],
        )
        self.assertEqual(CHECKED_AT, row["discontinued_at"])

    def test_missing_available_flag_is_a_parse_error_not_unavailable(self) -> None:
        payload = variation_payload(
            source_available=True,
            webshop_stock=1,
            aarhus_stock=1,
        )
        del payload["options"]["color"]["color-1"]["available"]

        with self.assertRaises(KaufmannVariationParseError):
            variation_rows_from_payload(
                payload,
                "parent-1",
                "https://www.kaufmann.dk/produkt/example",
            )

    def test_extracts_parent_id_from_decoded_page_state(self) -> None:
        source_parent_id = "019b0e1185a8735cbc2b41285d6be759"
        page_html = (
            "<div x-init=\"$store.productStore.setup('"
            + source_parent_id
            + "', 'color-id', 'DKK 499')\"></div>"
        )
        self.assertEqual(source_parent_id, extract_source_parent_id(page_html))
        self.assertEqual(
            source_parent_id,
            extract_source_parent_id(
                "{&quot;parentId&quot;:&quot;" + source_parent_id + "&quot;}"
            ),
        )

    def test_price_and_url_normalization(self) -> None:
        self.assertEqual(1499.95, parse_price("DKK 1.499,95"))
        self.assertEqual(
            "https://www.kaufmann.dk/produkt/example",
            clean_product_url(
                "https://www.kaufmann.dk/produkt/example?color=abc#color=abc"
            ),
        )


def refresh(rows, payload=None, *, error=None, candidates=None):
    """Run the shared-path adapter against staged rows and a fake variation client."""
    staged = StagedClient("kaufmann", rows)
    client = Mock()
    if error is not None:
        client.fetch_payload.side_effect = error
    else:
        client.fetch_payload.return_value = payload
    args = parse_args(["--store", "kaufmann", "--no-delay"])
    with patch.object(kaufmann, "KaufmannVariationClient", return_value=client):
        stats = kaufmann.refresh_store(staged, args, candidates=candidates)
    return staged, stats


class KaufmannRefreshLifecycleTests(unittest.TestCase):
    def test_unknown_available_colour_is_a_catalog_candidate_not_a_new_row(self) -> None:
        payload = variation_payload(source_available=True, webshop_stock=1, aarhus_stock=1)
        candidates = set()
        staged, _ = refresh([product_row(source_color_id="color-1")], payload, candidates=candidates)
        self.assertEqual(set(), candidates)
        payload["options"]["color"]["new-colour"] = payload["options"]["color"]["color-1"]
        staged, _ = refresh([product_row(source_color_id="color-1")], payload, candidates=candidates)
        self.assertEqual({"https://www.kaufmann.dk/produkt/example"}, candidates)
        self.assertEqual([785], [patch["id"] for patch in staged.patches])

    def test_missing_color_requires_two_successful_confirmations(self) -> None:
        payload = variation_payload(source_available=True, webshop_stock=1, aarhus_stock=1)
        staged, _ = refresh([product_row(id=1, source_color_id="gone")], payload)
        first = staged.patches[0]["payload"]
        self.assertEqual(1, first["consecutive_source_misses"])
        self.assertNotIn("publication_status", first)
        self.assertNotIn("local_inventory", first)
        self.assertEqual([], staged.history)

        staged, stats = refresh(
            [product_row(id=1, source_color_id="gone", consecutive_source_misses=1)], payload)
        second = staged.patches[0]["payload"]
        self.assertEqual(2, second["consecutive_source_misses"])
        self.assertEqual("unavailable", second["publication_status"])
        self.assertEqual("color_missing_from_variation_twice", second["status_reason"])
        self.assertFalse(second["source_available"])
        self.assertEqual("color_missing", staged.history[0]["refresh_status"])
        self.assertEqual(1, stats.unavailable)

    def test_404_style_unavailable_keeps_first_discontinued_time_and_price(self) -> None:
        row = product_row(discontinued_at="2026-08-25T08:00:00+00:00", current_price=299.0, list_price=None,
                          local_inventory={"stores": {"aarhus-c": {"stock_known": True, "sizes": {"M": {"stock": 2}}}}})
        error = KaufmannVariationUnavailable(404, "https://www.kaufmann.dk/widgets/product/variation/parent-1")
        staged, _ = refresh([row], error=error)
        payload = staged.patches[0]["payload"]
        self.assertEqual("unavailable", payload["publication_status"])
        self.assertEqual("variation_http_404", payload["status_reason"])
        self.assertEqual("2026-08-25T08:00:00+00:00", payload["discontinued_at"])
        self.assertEqual(299.0, payload["current_price"])
        self.assertEqual([], payload["webshop_sizes"])
        self.assertFalse(payload["aarhus_available"])
        self.assertEqual(0, payload["local_inventory"]["stores"]["aarhus-c"]["sizes"]["M"]["stock"])
        self.assertEqual("page_unavailable", staged.history[0]["refresh_status"])

    def test_legacy_journaled_row_without_prices_never_nulls_them(self) -> None:
        error = KaufmannVariationUnavailable(404, "https://www.kaufmann.dk/produkt/example")
        staged, _ = refresh([product_row()], error=error)
        self.assertNotIn("current_price", staged.patches[0]["payload"])
        self.assertEqual("product_page_http_404", staged.patches[0]["payload"]["status_reason"])

    def test_source_unavailable_colour_writes_lifecycle_and_history(self) -> None:
        payload = variation_payload(source_available=False, webshop_stock=0, aarhus_stock=0)
        first_discontinued_at = "2026-08-20T08:00:00+00:00"
        staged, stats = refresh([product_row(discontinued_at=first_discontinued_at)], payload)
        patch_payload = staged.patches[0]["payload"]
        history = staged.history[0]
        self.assertFalse(patch_payload["source_available"])
        self.assertEqual("unavailable", patch_payload["publication_status"])
        self.assertEqual(first_discontinued_at, patch_payload["discontinued_at"])
        self.assertNotIn("source_url", patch_payload)
        self.assertFalse(history["source_available"])
        self.assertEqual("unavailable", history["publication_status"])
        self.assertEqual("source_unavailable", history["refresh_status"])
        self.assertEqual(1, history["observation_count"])
        self.assertNotIn("webshop_sizes", history)
        self.assertEqual(1, stats.unavailable)

    def test_parse_failure_retains_existing_rows(self) -> None:
        staged, stats = refresh([product_row()], error=KaufmannVariationParseError("bad payload"))
        self.assertEqual([], staged.patches)
        self.assertEqual(1, stats.failed)


if __name__ == "__main__":
    unittest.main()
