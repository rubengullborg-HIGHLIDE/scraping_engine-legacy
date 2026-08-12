from __future__ import annotations

import unittest

from scrapers.full_import.shoechapter import ShoeChapterScraper


class ShoeChapterScraperTests(unittest.TestCase):
    def test_inventory_parses_exact_aarhus_stock(self) -> None:
        variants = [
            {"id": 1, "title": "42"},
            {"id": 2, "title": "43"},
        ]
        inventory, by_variant, raw = ShoeChapterScraper._inventory_from_page("""
          <variant-inventory>
            <span data-variant-id="1" data-status="warning">
              <strong>Lagerstatus i butik</strong><br>
              <span>Store Torv 6, 8000 Aarhus</span>
              <span>Kun 1 enhed tilbage</span>
            </span>
            <span data-variant-id="2" data-status="warning">
              <span>Kun 2 enheder tilbage</span>
            </span>
          </variant-inventory>
        """, variants)

        store = inventory["stores"]["shoechapter-aarhus"]
        self.assertTrue(store["stock_known"])
        self.assertTrue(store["available"])
        self.assertEqual(3, store["total_stock"])
        self.assertEqual(1, store["sizes"]["42"]["stock"])
        self.assertEqual(2, by_variant["2"]["stock"])
        self.assertEqual(1, raw["1"]["stock"])

    def test_row_summaries_split_known_sum_from_exact_total(self) -> None:
        scraper = ShoeChapterScraper()
        product = {
            "id": 10,
            "handle": "example-shoe",
            "title": "Example Shoe - Black",
            "vendor": "Example",
            "type": "Sneakers",
            "price": 10000,
            "variants": [
                {"id": 1, "title": "42", "sku": "EX-42", "available": True},
                {"id": 2, "title": "43", "sku": "EX-43", "available": True},
            ],
            "tags": [],
            "images": [],
        }

        row = scraper.product_to_row(product, """
          <variant-inventory>
            <span data-variant-id="1" data-status="warning">
              <span>Kun 2 enheder tilbage</span>
            </span>
            <span data-variant-id="2" data-status="success">
              <span>På lager</span>
            </span>
          </variant-inventory>
        """)

        self.assertEqual(2, row["local_total_stock"])
        self.assertIsNone(row["aarhus_total_stock"])
        self.assertTrue(row["aarhus_available"])

    def test_color_handles_from_other_colours(self) -> None:
        handles = ShoeChapterScraper.color_handles("""
          <div class="product-single__colors">
            <a href="/products/current"><div class="colors__item active"></div></a>
            <a href="/products/other-colour?variant=1"><div class="colors__item"></div></a>
          </div>
        """)

        self.assertEqual(["current", "other-colour"], handles)

    def test_size_guide_table_is_structured(self) -> None:
        details = ShoeChapterScraper._page_details("""
          <details class="accordion">
            <summary>Beskrivelse</summary>
            <div class="accordion__content">
              <p>Main text.</p>
              <ul><li>Overdel i ruskind og mesh</li><li>Almindelige i størrelsen</li></ul>
            </div>
          </details>
          <div class="sizeguide_content">
            <table>
              <tr><td colspan="4"><strong>New Balance Unisex Size Guide</strong></td></tr>
              <tr><td><strong>EUR</strong></td><td><strong>US</strong></td><td><strong>UK</strong></td><td><strong>JP (CM)</strong></td></tr>
              <tr><td>42</td><td>8.5</td><td>8</td><td>26.5</td></tr>
            </table>
          </div>
        """)

        self.assertEqual("Main text.", details["description"])
        self.assertEqual(["Overdel i ruskind og mesh", "Almindelige i størrelsen"], details["highlights"])
        self.assertEqual("New Balance Unisex Size Guide", details["size_guide"]["title"])
        self.assertEqual({"EUR": "42", "US": "8.5", "UK": "8", "JP (CM)": "26.5"}, details["size_guide"]["rows"][0])

    def test_style_reference_removes_size_suffix(self) -> None:
        self.assertEqual("KJ9980", ShoeChapterScraper._style_reference([{"sku": "KJ9980-40-2-3", "title": "40 2/3"}], 1))

    def test_materials_keep_source_evidence(self) -> None:
        materials, sources = ShoeChapterScraper._materials([
            "Overdel i ruskind og mesh",
            "Premium nubuckoverdel",
            "Tåkap i sort gummi",
            "LIGHTSTRIKE PRO skumsål",
            "Ydersål i gummi",
            "God komfort",
        ])

        self.assertEqual([
            "Overdel i ruskind og mesh",
            "Premium nubuckoverdel",
            "Tåkap i sort gummi",
            "LIGHTSTRIKE PRO skumsål",
            "Ydersål i gummi",
        ], materials)
        self.assertEqual(materials, sources)


if __name__ == "__main__":
    unittest.main()
