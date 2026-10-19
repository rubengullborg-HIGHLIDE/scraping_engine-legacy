from __future__ import annotations

import unittest
from unittest.mock import Mock

from scrapers.full_import.skagen_clothing import MEN_COLLECTION_HANDLE, SkagenClothingScraper
from scrapers.source_http import SourcePayloadError


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
                {"id": 1, "handle": "shirt", "product_type": "T-shirts", "tags": ["ALT TØJ TIL MÆND"]},
                {"id": 2, "handle": "tank", "product_type": "Tank top", "tags": ["ALT TØJ TIL MÆND", "ACCESSORIES"]},
                {"id": 3, "handle": "beanie", "product_type": "Beanie"},
                {"id": 4, "handle": "gift", "product_type": "Gavekort"},
                {"id": 5, "handle": "cap", "product_type": "Caps", "tags": ["ALT TØJ TIL MÆND"]},
                {"id": 6, "handle": "women-shirt", "product_type": "T-shirts", "tags": ["ALT KVINDETØJ"]},
            ])
        return _Response([])


class SkagenClothingDiscoveryTests(unittest.TestCase):
    def test_empty_and_malformed_discovery_are_failures(self):
        for payload in ({'products': []}, {}, {'products': None}, []):
            session = Mock(headers={})
            session.get.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertRaises(SourcePayloadError):
                SkagenClothingScraper(session).discover_products()

    def test_discovery_keeps_clothing_and_tank_tops(self) -> None:
        self.assertEqual(MEN_COLLECTION_HANDLE, 'alt')
        products = SkagenClothingScraper(_Session()).discover_products()

        self.assertEqual(["shirt", "tank"], [product["handle"] for product in products])


if __name__ == "__main__":
    unittest.main()
