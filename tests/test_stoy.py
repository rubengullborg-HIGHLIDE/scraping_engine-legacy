from __future__ import annotations

import unittest

from scrapers.full_import.stoy import StoyScraper


class StoyScraperTests(unittest.TestCase):
    def test_product_row_accepts_dkk_and_converts_shopify_subunits(self) -> None:
        row = StoyScraper().product_to_row(
            {
                "id": 1,
                "handle": "pants",
                "title": "Pants",
                "price": 740000,
                "compare_at_price": None,
                "variants": [],
            },
            """
              <meta property="og:price:currency" content="DKK">
              <script id="stape-product-data" type="application/json">{"metafields": {"custom": {}}}</script>
            """,
        )

        self.assertEqual(7400.0, row["current_price"])
        self.assertEqual("DKK", row["currency"])
        self.assertEqual("DK", row["raw"]["market_country"])
        self.assertEqual("DKK", row["raw"]["page_currency"])

    def test_product_row_rejects_non_dkk_market_price(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected DKK, got EUR"):
            StoyScraper().product_to_row(
                {
                    "id": 1,
                    "handle": "pants",
                    "price": 94295,
                    "variants": [],
                },
                '<meta property="og:price:currency" content="EUR">',
            )

    def test_page_currency_falls_back_to_shopify_currency_state(self) -> None:
        self.assertEqual(
            "DKK",
            StoyScraper._page_currency(
                '<script>Shopify.currency = {"active":"DKK","rate":"1.0"};</script>'
            ),
        )

    def test_store_availability_has_two_stores_and_no_quantities(self) -> None:
        product_data, inventory, raw = StoyScraper._page_data("""
          <script id="stape-product-data" type="application/json">{"metafields": {"custom": {}}}</script>
          <div class="store-availability-drawer__store">
            <h6 class="store-availability-drawer__name">Aarhus Store</h6>
            <p class="store-availability-drawer__address">Store Torv 4</p>
            <li class="store-availability-drawer__size is-available"><span class="store-availability-drawer__size-label">M</span></li>
            <li class="store-availability-drawer__size is-unavailable"><span class="store-availability-drawer__size-label">L</span></li>
          </div>
          <div class="store-availability-drawer__store">
            <h6 class="store-availability-drawer__name">Copenhagen Store</h6>
            <li class="store-availability-drawer__size is-unavailable"><span class="store-availability-drawer__size-label">M</span></li>
          </div>
        """)
        self.assertEqual({}, product_data["metafields"]["custom"])
        self.assertTrue(inventory["stores"]["stoy-aarhus"]["available"])
        self.assertFalse(inventory["stores"]["stoy-copenhagen"]["available"])
        self.assertIsNone(inventory["stores"]["stoy-aarhus"]["sizes"]["M"]["stock"])
        self.assertFalse(inventory["stores"]["stoy-aarhus"]["stock_known"])
        self.assertEqual(True, raw["stoy-aarhus"]["sizes"]["M"])

    def test_style_reference_removes_colour_suffix(self) -> None:
        self.assertEqual("1032509", StoyScraper._style_reference("1032509-charcoal", [], 1))

    def test_specifications_use_stoy_product_metafields(self) -> None:
        specifications = StoyScraper._specifications({
            "composition": "100% wool",
            "country": "Portugal",
            "category_stoy": ["Clothing", "Knitwear"],
            "manufacturer_style_code": "ABC-001",
            "included_in_collections": True,
        })

        self.assertEqual("100% wool", specifications["composition"])
        self.assertEqual(["Clothing", "Knitwear"], specifications["category_stoy"])
        self.assertNotIn("included_in_collections", specifications)

    def test_size_and_fit_parses_model_and_fit_guidance(self) -> None:
        model_info, size_guide = StoyScraper._size_and_fit("""
          <div class="product__accordion">
            <summary>Størrelse &amp; Pasform</summary>
            <div class="accordion__content">
              Modellen er 183 cm / 6’0” og bærer størrelse 31/32<br>
              Passer til størrelsen
            </div>
          </div>
        """)

        self.assertEqual(183, model_info["height_cm"])
        self.assertEqual("6’0”", model_info["height_imperial"])
        self.assertEqual("31/32", model_info["wears_size"])
        self.assertEqual(["Passer til størrelsen"], size_guide["fit_guidance"])


if __name__ == "__main__":
    unittest.main()
