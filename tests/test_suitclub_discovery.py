from __future__ import annotations

import unittest

from scrapers.full_import.suitclub import SuitClubScraper


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

    def get(self, url: str, **kwargs) -> _Response:
        page = (kwargs.get("params") or {}).get("page")
        if page != 1:
            return _Response([])
        if "/enkelte-dele/" in url:
            return _Response([
                {"id": 1, "handle": "blazer", "product_type": "Blazer"},
                {"id": 2, "handle": "pants", "product_type": "Suit pants"},
                {"id": 3, "handle": "vest", "product_type": "Veste"},
                {"id": 4, "handle": "bundle", "product_type": "Two-piece suit"},
            ])
        if "/toej/" in url:
            return _Response([
                {"id": 5, "handle": "shirt", "product_type": "Skjorter"},
                {"id": 6, "handle": "knit", "product_type": "Strik"},
                {"id": 7, "handle": "tee", "product_type": "T-shirt"},
                {"id": 8, "handle": "tie", "product_type": "Slips"},
            ])
        if "/sko-til-jakkesaet/" in url:
            return _Response([
                {"id": 9, "handle": "shoe", "product_type": "Sko"},
                {"id": 1, "handle": "blazer", "product_type": "Blazer"},
            ])
        return _Response([])


class SuitClubDiscoveryTests(unittest.TestCase):
    def test_discovery_combines_clothing_and_footwear_but_excludes_other_types(self) -> None:
        products = SuitClubScraper(_Session()).discover_products()

        self.assertEqual(
            ["blazer", "pants", "vest", "shirt", "knit", "tee", "shoe"],
            [product["handle"] for product in products],
        )
        self.assertEqual(
            ["enkelte-dele", "sko-til-jakkesaet"],
            products[0]["_discovery_collections"],
        )

    def test_single_collection_override_remains_supported(self) -> None:
        products = SuitClubScraper(_Session()).discover_products("toej")

        self.assertEqual(["shirt", "knit", "tee"], [product["handle"] for product in products])


if __name__ == "__main__":
    unittest.main()
