from __future__ import annotations

import unittest

from scrapers.full_import.romerhus import RomerhusScraper


class _Response:
    def __init__(self, products: list[dict]) -> None:
        self._products = products

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"products": self._products}


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs) -> _Response:
        handle = url.split("/collections/", 1)[1].split("/", 1)[0]
        self.calls.append(handle)
        products = {
            "overtoj-maend": [
                {"id": 1, "handle": "existing-jacket", "tags": []},
            ],
            "nyheder-test": [
                {"id": 1, "handle": "existing-jacket", "tags": ["News-mænd", "Outerwear_Jacket"]},
                {"id": 2, "handle": "new-knit", "tags": ["News-mænd", "Knit_Pullover"]},
                {"id": 3, "handle": "new-socks", "tags": ["News-mænd", "Socks_Socks", "accessoriesmænd"]},
                {"id": 4, "handle": "new-loafers", "tags": ["News-mænd", "Footwear_Loafers"]},
                {"id": 5, "handle": "new-bag", "tags": ["News-mænd", "Bags_Bag"]},
                {"id": 6, "handle": "new-cap", "tags": ["News-mænd", "Headwear_Cap"]},
                {"id": 7, "handle": "not-mens-news", "tags": ["Knit_Pullover"]},
            ],
        }.get(handle, [])
        return _Response(products)


class RomerhusDiscoveryTests(unittest.TestCase):
    def test_default_discovery_adds_eligible_news_and_excludes_accessories(self) -> None:
        session = _Session()

        products = RomerhusScraper(session).discover_products()

        self.assertIn("nyheder-test", session.calls)
        self.assertEqual(
            ["existing-jacket", "new-knit", "new-socks", "new-loafers"],
            [product["handle"] for product in products],
        )

    def test_news_filter_requires_mens_news_and_an_allowed_category(self) -> None:
        self.assertTrue(RomerhusScraper.is_news_catalog_product({
            "tags": ["News-mænd", "T-Shirts & Tops_T-shirt"],
        }))
        self.assertTrue(RomerhusScraper.is_news_catalog_product({
            "tags": ["News-mænd", "Socks_Socks", "accessoriesmænd"],
        }))
        self.assertFalse(RomerhusScraper.is_news_catalog_product({
            "tags": ["News-mænd", "Other Accessories_Tie"],
        }))
        self.assertFalse(RomerhusScraper.is_news_catalog_product({
            "tags": ["Knit_Pullover"],
        }))


if __name__ == "__main__":
    unittest.main()
