from __future__ import annotations

import unittest

from scrapers.full_import.lakor import LakorScraper


class LakorScraperTests(unittest.TestCase):
    def test_generic_store_message_means_all_tracked_stores(self) -> None:
        availability = LakorScraper._store_availability_from_html(
            '<p class="product-availability"><span class="availability-dot">•</span>'
            '<span class="availability-text">Også på lager&nbsp;i <a href="/pages/stores">vores butikker</a></span></p>'
        )

        self.assertTrue(availability["available"])
        self.assertFalse(availability["low_stock"])
        self.assertEqual("all_tracked_stores", availability["store_scope"])
        self.assertEqual(
            ["lakor-aalborg", "lakor-aarhus", "lakor-copenhagen"],
            availability["stores"],
        )

    def test_named_low_stock_message_only_assigns_named_stores(self) -> None:
        availability = LakorScraper._store_availability_from_html(
            '''<p class="product-availability"><span class="availability-text">
                Kun få tilbage i <a href="/pages/lakor-shop-aalborg">butik Aalborg</a>
                og <a href="/pages/lakor-shop-copenhagen">butik København</a>
            </span></p>'''
        )

        self.assertTrue(availability["available"])
        self.assertTrue(availability["low_stock"])
        self.assertEqual("named_stores", availability["store_scope"])
        self.assertEqual(["lakor-aalborg", "lakor-copenhagen"], availability["stores"])

    def test_empty_section_means_no_shop_has_the_size(self) -> None:
        availability = LakorScraper._store_availability_from_html(
            '<div id="shopify-section-store-availability"><p class="product-availability"></p></div>'
        )
        self.assertFalse(availability["available"])
        self.assertEqual([], availability["stores"])

    def test_missing_section_or_unknown_wording_raises(self) -> None:
        from scrapers.source_http import SourcePayloadError

        for html in (
            "<html><body>Page not found</body></html>",
            '<p class="product-availability"><span class="availability-text">Ny formulering</span></p>',
            '<p class="product-availability"><span class="availability-text">På lager i butik Ukendt</span></p>',
        ):
            with self.subTest(html=html), self.assertRaises(SourcePayloadError):
                LakorScraper._store_availability_from_html(html)


if __name__ == "__main__":
    unittest.main()
