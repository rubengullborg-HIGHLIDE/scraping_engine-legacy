from __future__ import annotations

import unittest

from scrapers.full_import.stoy import StoyScraper


class StoyScraperTests(unittest.TestCase):
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
