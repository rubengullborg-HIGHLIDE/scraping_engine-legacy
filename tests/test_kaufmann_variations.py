from __future__ import annotations

import unittest

from scrapers.stores.kaufmann_variations import (
    KaufmannVariationParseError,
    clean_product_url,
    extract_source_parent_id,
    parse_price,
    variation_rows_from_payload,
)
from scripts.refresh_kaufmann_inventory import (
    dynamic_product_upsert_row,
    missing_color_product_payload,
    refresh_status_for_row,
    snapshot_payload,
    unavailable_product_payload,
)


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
        self.assertEqual(
            3,
            row["aarhus_inventory"]["stores"]["bruuns-galleri"]["sizes"]["M"][
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
            row["aarhus_inventory"]["stores"]["bruuns-galleri"]["available"]
        )
        self.assertEqual(
            0,
            row["aarhus_inventory"]["stores"]["bruuns-galleri"]["sizes"]["M"][
                "stock"
            ],
        )
        self.assertEqual(CHECKED_AT, row["discontinued_at"])
        self.assertEqual("source_unavailable", refresh_status_for_row(row))

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


class KaufmannRefreshLifecycleTests(unittest.TestCase):
    def test_missing_color_requires_two_successful_confirmations(self) -> None:
        first_payload, first_confirmed = missing_color_product_payload(
            product_row(),
            CHECKED_AT,
        )
        self.assertFalse(first_confirmed)
        self.assertEqual(1, first_payload["consecutive_source_misses"])
        self.assertNotIn("publication_status", first_payload)

        second_payload, second_confirmed = missing_color_product_payload(
            product_row(consecutive_source_misses=1),
            CHECKED_AT,
        )
        self.assertTrue(second_confirmed)
        self.assertEqual(2, second_payload["consecutive_source_misses"])
        self.assertEqual("unavailable", second_payload["publication_status"])
        self.assertEqual(
            "color_missing_from_variation_twice",
            second_payload["status_reason"],
        )

    def test_404_style_unavailable_payload_keeps_first_discontinued_time(self) -> None:
        payload = unavailable_product_payload(
            product_row(discontinued_at="2026-08-25T08:00:00+00:00"),
            CHECKED_AT,
            "variation_http_404",
        )
        self.assertEqual("unavailable", payload["publication_status"])
        self.assertEqual("variation_http_404", payload["status_reason"])
        self.assertEqual("2026-08-25T08:00:00+00:00", payload["discontinued_at"])
        self.assertEqual([], payload["webshop_sizes"])
        self.assertFalse(payload["aarhus_available"])

    def test_upsert_and_snapshot_include_lifecycle_fields(self) -> None:
        scraped = variation_rows_from_payload(
            variation_payload(
                source_available=False,
                webshop_stock=0,
                aarhus_stock=0,
            ),
            "parent-1",
            "https://www.kaufmann.dk/produkt/example",
            checked_at=CHECKED_AT,
        )[0]
        row = product_row()
        upsert = dynamic_product_upsert_row(row, scraped)
        snapshot = snapshot_payload(
            row,
            scraped,
            CHECKED_AT,
            refresh_status="source_unavailable",
        )

        self.assertFalse(upsert["source_available"])
        self.assertEqual("unavailable", upsert["publication_status"])
        self.assertFalse(snapshot["source_available"])
        self.assertEqual("unavailable", snapshot["publication_status"])
        self.assertEqual("source_unavailable", snapshot["refresh_status"])

    def test_repeat_unavailable_refresh_preserves_first_discontinued_time(self) -> None:
        scraped = variation_rows_from_payload(
            variation_payload(
                source_available=False,
                webshop_stock=0,
                aarhus_stock=0,
            ),
            "parent-1",
            "https://www.kaufmann.dk/produkt/example",
            checked_at=CHECKED_AT,
        )[0]
        first_discontinued_at = "2026-08-20T08:00:00+00:00"

        upsert = dynamic_product_upsert_row(
            product_row(discontinued_at=first_discontinued_at),
            scraped,
        )

        self.assertEqual(first_discontinued_at, upsert["discontinued_at"])


if __name__ == "__main__":
    unittest.main()
