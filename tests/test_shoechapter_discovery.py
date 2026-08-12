from __future__ import annotations

import unittest

from scrapers.full_import.shoechapter import ShoeChapterScraper


class _Response:
    def __init__(self, products: list[dict]) -> None:
        self._products = products

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"products": self._products}


class _Session:
    def __init__(self) -> None:
        self.headers = {}
        self.calls = 0

    def get(self, *args, **kwargs) -> _Response:
        self.calls += 1
        if self.calls == 1:
            return _Response([
                {"id": 1, "handle": "shoe", "product_type": "Sneakers"},
                {"id": 2, "handle": "socks", "product_type": "Sokker"},
                {"id": 3, "handle": "magazine", "product_type": "Magasiner"},
            ])
        return _Response([])


class ShoeChapterDiscoveryTests(unittest.TestCase):
    def test_discovery_keeps_only_sneakers(self) -> None:
        products = ShoeChapterScraper(_Session()).discover_products()

        self.assertEqual(["shoe"], [product["handle"] for product in products])


if __name__ == "__main__":
    unittest.main()
