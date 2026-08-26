from __future__ import annotations

import json
import unittest

from scrapers.full_import.suitclub import SuitClubScraper


class SuitClubScraperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product = {
            "id": 100,
            "handle": "prestige-navy-habitbukser",
            "title": "PRESTIGE navy habitbukser",
            "vendor": "MBO",
            "product_type": "Suit pants",
            "options": [
                {"name": "Pasform", "values": ["Regular fit", "Straight fit"]},
                {"name": "Størrelse", "values": ["48 (M)"]},
            ],
            "variants": [
                {
                    "id": 1,
                    "title": "Regular fit / 48 (M)",
                    "option1": "Regular fit",
                    "option2": "48 (M)",
                    "sku": "REG-48",
                    "price": "699.00",
                    "available": True,
                },
                {
                    "id": 2,
                    "title": "Straight fit / 48 (M)",
                    "option1": "Straight fit",
                    "option2": "48 (M)",
                    "sku": "STR-48",
                    "price": "699.00",
                    "available": True,
                },
            ],
            "images": ["//cdn.example/pants.jpg"],
            "tags": ["PRESTIGE"],
        }
        self.locations = [
            self._location("10", "Lager Aarhus", "Jens Juuls Vej 12", "8260", "Viby J"),
            self._location("11", "Butik Aarhus", "Guldsmedgade 42", "8000", "Aarhus C"),
            self._location("12", "Butik København", "Bredgade 21", "1260", "København K"),
            self._location("13", "Butik Odense", "Kongensgade 2", "5000", "Odense C"),
            self._location("14", "Butik Aalborg", "Slotsgade 2", "9000", "Aalborg"),
        ]
        self.inventory_node = {
            "id": "gid://shopify/Product/100",
            "variants": {"nodes": [
                self._inventory_variant(1, {"10": 10, "11": 3, "12": 2, "13": 0, "14": 1}),
                self._inventory_variant(2, {"10": 0, "11": 0, "12": 1, "13": 0, "14": 0}),
            ]},
        }

    @staticmethod
    def _location(identifier: str, name: str, address: str, zip_code: str, city: str) -> dict:
        return {
            "id": f"gid://shopify/Location/{identifier}",
            "name": name,
            "address": {"address1": address, "address2": "", "zip": zip_code, "city": city},
        }

    @staticmethod
    def _inventory_variant(identifier: int, stocks: dict[str, int]) -> dict:
        return {
            "id": f"gid://shopify/ProductVariant/{identifier}",
            "storeAvailability": {"nodes": [
                {
                    "available": stock > 0,
                    "quantityAvailable": stock,
                    "location": {"id": f"gid://shopify/Location/{location_id}"},
                }
                for location_id, stock in stocks.items()
            ]},
        }

    @staticmethod
    def _page_html() -> str:
        payload = {
            "id": 100,
            "title": "PRESTIGE navy habitbukser",
            "handle": "prestige-navy-habitbukser",
            "vendor": "MBO",
            "type": "Suit pants",
            "price": 69900,
            "compare_at_price": 0,
            "tags": ["PRESTIGE"],
            "category": "Ikke kategoriseret",
            "collections": [{"id": 20, "title": "Enkeltdele", "handle": "enkelte-dele"}],
            "metafields": {"custom": {
                "beskrivelse": (
                    "Produktbeskrivelse.\nMateriale\n62% polyester\n33% viskose\n5% elastan\n"
                    "Detaljer\nPressefolder\nDansk design\nStørrelse og pasform\n"
                    "Model 185 cm og 74 kg bruger størrelse 48 (M)\nSUIT CLUB fit - normal pasform."
                ),
                "blazer": {"id": 200, "title": "Matching blazer", "handle": "matching-blazer"},
                "color": [{"id": 100, "title": "Navy pants", "handle": "prestige-navy-habitbukser"}],
                "farve": "Navy",
                "kollektion": "Prestige",
                "model": "Model: 185 cm, 74 kg bruger størrelse 48 (M)",
                "m_nster": "Plain",
            }},
        }
        return f'<script type="application/json" id="stape-product-data">{json.dumps(payload)}</script>'

    def test_storefront_config_ignores_template_url(self) -> None:
        html = """
          https://${this.config.shopDomain}/api/2025-10/graphql.json
          https://suiteclub-dk.myshopify.com/api/2024-10/graphql.json
          'X-Shopify-Storefront-Access-Token': 'public-token'
        """

        endpoint, token = SuitClubScraper._storefront_config(html)

        self.assertEqual("https://suiteclub-dk.myshopify.com/api/2024-10/graphql.json", endpoint)
        self.assertEqual("public-token", token)

    def test_page_details_extract_product_specific_sections_and_relations(self) -> None:
        details = SuitClubScraper._page_details(self._page_html())

        self.assertEqual("Produktbeskrivelse.", details["description"])
        self.assertEqual(["62% polyester", "33% viskose", "5% elastan"], details["materials"])
        self.assertEqual(["Pressefolder", "Dansk design"], details["highlights"])
        self.assertEqual(185, details["model_info"]["height_cm"])
        self.assertEqual("200", details["matching_products"]["blazer"]["source_product_id"])
        self.assertTrue(details["related_colors"][0]["current"])

    def test_inventory_separates_online_warehouse_and_physical_shops(self) -> None:
        result = SuitClubScraper._inventory_from_snapshot(
            self.product,
            self.product["variants"],
            self.inventory_node,
            self.locations,
        )

        aarhus = result["local_inventory"]["stores"]["suitclub-aarhus"]
        copenhagen = result["local_inventory"]["stores"]["suitclub-copenhagen"]
        self.assertEqual(3, aarhus["total_stock"])
        self.assertEqual(3, copenhagen["total_stock"])
        self.assertEqual(3, aarhus["sizes"]["Regular fit / 48 (M)"]["stock"])
        self.assertEqual(10, result["webshop_sizes"][0]["stock"])
        self.assertEqual("Straight fit", result["webshop_sizes"][1]["fit"])

    def test_row_uses_atomic_product_price_and_exact_summaries(self) -> None:
        snapshot = {
            "api_version": "2024-10",
            "locations": self.locations,
            "products": {"100": self.inventory_node},
        }

        row = SuitClubScraper().product_to_row(self.product, self._page_html(), snapshot)

        self.assertEqual("100", row["source_product_id"])
        self.assertEqual(699.0, row["current_price"])
        self.assertEqual("Regular fit / Straight fit", row["fit"])
        self.assertEqual("Trousers", row["category"])
        self.assertEqual(["Clothing", "Suits", "Trousers"], row["category_path"])
        self.assertEqual(3, row["aarhus_total_stock"])
        self.assertEqual(7, row["local_total_stock"])
        self.assertTrue(row["aarhus_available"])


if __name__ == "__main__":
    unittest.main()
