from __future__ import annotations

import unittest

from scrapers.full_import.stoy import StoyScraper


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.headers = {}
        self.calls: list[str] = []

    def get(self, url: str, **_: object) -> _Response:
        self.calls.append(url)
        if "all-clothing-for-men" in url:
            return _Response({"products": [{"id": 1, "handle": "shirt"}]})
        return _Response({"products": [{"id": 1, "handle": "shirt"}, {"id": 2, "handle": "shoe"}]})


class StoyDiscoveryTests(unittest.TestCase):
    def test_default_discovery_unions_clothing_and_footwear(self) -> None:
        scraper = StoyScraper(session=_Session())
        products = scraper.discover_products()

        self.assertEqual([1, 2], [product["id"] for product in products])
        self.assertEqual(
            ["all-clothing-for-men", "footwear-for-men"],
            products[0]["_discovery_collections"],
        )


if __name__ == "__main__":
    unittest.main()
