from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scrapers.full_import.kaufmann import KaufmanScraper
from scripts.sync_kaufmann_catalog import (
    candidate_urls_from_file,
    normalized_known_urls,
)


class KaufmannCatalogSyncTests(unittest.TestCase):
    def test_sitemap_only_scraper_does_not_launch_browser(self) -> None:
        scraper = KaufmanScraper(launch_browser=False)
        try:
            self.assertIsNone(scraper._browser)
            self.assertIsNone(scraper._playwright)
        finally:
            scraper.close()

    def test_known_urls_are_canonicalized_and_deduplicated(self) -> None:
        scraper = KaufmanScraper(launch_browser=False)
        try:
            rows = [
                {
                    "canonical_url": "https://www.kaufmann.dk/produkt/item-1?color=red#color=red",
                    "source_url": None,
                },
                {
                    "canonical_url": "https://www.kaufmann.dk/produkt/item-1?color=blue",
                    "source_url": None,
                },
                {
                    "canonical_url": None,
                    "source_url": "https://www.kaufmann.dk/produkt/item-2#color=green",
                },
            ]

            self.assertEqual(
                {
                    "https://www.kaufmann.dk/produkt/item-1",
                    "https://www.kaufmann.dk/produkt/item-2",
                },
                normalized_known_urls(scraper, rows),
            )
        finally:
            scraper.close()

    def test_unknown_colour_candidate_urls_are_normalized(self) -> None:
        scraper = KaufmanScraper(launch_browser=False)
        try:
            with TemporaryDirectory() as directory:
                path = Path(directory) / "candidates.json"
                path.write_text(
                    '["https://www.kaufmann.dk/produkt/item-1?color=red", '
                    '"https://www.kaufmann.dk/produkt/item-1#color=blue"]',
                    encoding="utf-8",
                )

                self.assertEqual(
                    {"https://www.kaufmann.dk/produkt/item-1"},
                    candidate_urls_from_file(scraper, str(path)),
                )
        finally:
            scraper.close()


if __name__ == "__main__":
    unittest.main()
