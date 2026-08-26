from __future__ import annotations

import unittest

from scrapers.full_import.cejf import CejfScraper


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
                {"id": 1, "handle": "shirt", "tags": ["men"]},
                {"id": 2, "handle": "women-shirt", "tags": ["women"]},
            ])
        return _Response([])


class CejfDiscoveryTests(unittest.TestCase):
    def test_discovery_keeps_mens_products_only(self) -> None:
        products = CejfScraper(_Session()).discover_products()

        self.assertEqual(["shirt"], [product["handle"] for product in products])
        self.assertEqual(["men"], products[0]["_discovery_collections"])


if __name__ == "__main__":
    unittest.main()
