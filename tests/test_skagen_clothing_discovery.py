from __future__ import annotations

import unittest

from scrapers.full_import.skagen_clothing import SkagenClothingScraper


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
                {"id": 1, "handle": "shirt", "product_type": "T-shirts"},
                {"id": 2, "handle": "tank", "product_type": "Tank top"},
                {"id": 3, "handle": "beanie", "product_type": "Beanie"},
                {"id": 4, "handle": "gift", "product_type": "Gavekort"},
            ])
        return _Response([])


class SkagenClothingDiscoveryTests(unittest.TestCase):
    def test_discovery_keeps_clothing_and_tank_tops(self) -> None:
        products = SkagenClothingScraper(_Session()).discover_products()

        self.assertEqual(["shirt", "tank"], [product["handle"] for product in products])


if __name__ == "__main__":
    unittest.main()
