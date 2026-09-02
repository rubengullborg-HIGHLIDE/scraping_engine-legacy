from __future__ import annotations

import unittest

from scrapers.full_import.stoy import StoyScraper


class _Response:
    def __init__(self, payload: dict, text: str = "") -> None:
        self.payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class _Session:
    def __init__(self) -> None:
        self.headers = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, kwargs))
        if url.endswith(".js"):
            return _Response({"id": 1, "price": 10000, "variants": []})
        if "/products/" in url:
            return _Response({}, '<meta property="og:price:currency" content="DKK">')
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
        self.assertTrue(scraper.session.calls)
        for _, kwargs in scraper.session.calls:
            self.assertEqual("DK", kwargs["params"]["country"])

    def test_product_requests_force_the_danish_market(self) -> None:
        session = _Session()
        scraper = StoyScraper(session=session)

        scraper.fetch_product("shirt")
        scraper.fetch_product_page("shirt")

        self.assertEqual(2, len(session.calls))
        for _, kwargs in session.calls:
            self.assertEqual({"country": "DK"}, kwargs["params"])


if __name__ == "__main__":
    unittest.main()
