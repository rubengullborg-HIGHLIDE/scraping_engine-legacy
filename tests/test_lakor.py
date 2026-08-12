from __future__ import annotations

import unittest

from scrapers.full_import.lakor import LakorScraper


class LakorScraperTests(unittest.TestCase):
    def test_generic_store_message_means_all_tracked_stores(self) -> None:
        availability = LakorScraper._store_availability_from_html(
            '<p class="availability-text">Også på lager i vores butikker</p>'
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
            '''<p class="availability-text">
                Kun få tilbage i <a href="/pages/lakor-shop-aalborg">butik Aalborg</a>
                og <a href="/pages/lakor-shop-copenhagen">butik København</a>
            </p>'''
        )

        self.assertTrue(availability["available"])
        self.assertTrue(availability["low_stock"])
        self.assertEqual("named_stores", availability["store_scope"])
        self.assertEqual(["lakor-aalborg", "lakor-copenhagen"], availability["stores"])


if __name__ == "__main__":
    unittest.main()
