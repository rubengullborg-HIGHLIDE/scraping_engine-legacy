"""Takeoff Copenhagen (takeoffcph.dk): sneakers, streetwear and preloved.

Each Shopify product is one colourway; its variants are sizes. Exact per-shop
stock comes from Takeoff's own public endpoint, the same one its product
page uses for the "Available in-stock in N stores" drawer:

    GET https://sell.takeoffcph.dk/api/shopify/inventorylevels?q=<product id>

It returns Shopify ``storeAvailability`` per variant with ``quantityAvailable``
for "Takeoff Aarhus" and "Takeoff Copenhagen". Shopify's ``available`` flag
is meaningless for local stock here: Takeoff sells most sizes on 5-10 day
sourcing, so nearly every size is "available" online with zero in the shops.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import requests

from scrapers import shopify
from scrapers.inventory import unique_sizes
from scrapers.source_http import SourcePayloadError

BASE_URL = "https://takeoffcph.dk"
INVENTORY_URL = "https://sell.takeoffcph.dk/api/shopify/inventorylevels"
DISCOVERY_COLLECTION = "all"
INCLUDED_TYPES = {"Sneakers", "T-shirts", "Hoodies & Sweatshirts", "Jackets", "Pants & Shorts", "Preloved"}
TRACKED_STORES = {
    "Takeoff Aarhus": {"slug": "takeoff-aarhus", "name": "Takeoff Aarhus", "address": "Guldsmedgade 24A, 8000 Aarhus C"},
    "Takeoff Copenhagen": {"slug": "takeoff-copenhagen", "name": "Takeoff Copenhagen",
                           "address": "Vingårdstræde 20, 1070 København K"},
}
AARHUS_LOCATION = "Takeoff Aarhus"
CLOTHING_TYPES = {"T-shirts", "Hoodies & Sweatshirts", "Jackets", "Pants & Shorts"}


class TakeoffScraper:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.storefront = shopify.ShopifyStorefront(BASE_URL, session)
        self.session = self.storefront.session

    # Discovery and fetching -------------------------------------------------

    def discover_products(self, collection: str = DISCOVERY_COLLECTION) -> list[dict[str, Any]]:
        return self.storefront.collection_products(collection, keep=self.is_catalog_product)

    @staticmethod
    def is_catalog_product(product: dict[str, Any]) -> bool:
        return shopify.product_type(product) in INCLUDED_TYPES

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        return self.storefront.product_js(url_or_handle)

    def fetch_inventory(self, product_id: Any) -> dict[str, Any]:
        response = self.session.get(INVENTORY_URL, params={"q": str(product_id)}, timeout=30)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self.storefront.close()

    # Row building ------------------------------------------------------------

    def product_to_row(self, product: dict[str, Any], inventory_payload: dict[str, Any]) -> dict[str, Any]:
        product_id = str(product["id"])
        handle = shopify.text(product.get("handle"))
        name = shopify.text(product.get("title"))
        if not handle or not name:
            raise SourcePayloadError(f"Takeoff product {product_id} has no handle or title")
        variants = [variant for variant in product.get("variants") or [] if variant.get("id") is not None]
        if not variants:
            raise SourcePayloadError(f"Takeoff product {product_id} has no variants")
        sizes = unique_sizes(variants, lambda variant: self._size(product, variant), "Takeoff product")
        stock = self._stock_by_variant(product_id, inventory_payload, sizes)

        stores = {
            source["slug"]: {"name": source["name"], "address": source["address"], "stock_known": True,
                             "available": False, "total_stock": 0, "sizes": {}}
            for source in TRACKED_STORES.values()
        }
        webshop_sizes = []
        for variant in variants:
            variant_id = str(variant["id"])
            size = sizes[variant_id]
            for location, source in TRACKED_STORES.items():
                quantity = stock[variant_id][location]
                store = stores[source["slug"]]
                store["sizes"][size] = {"available": quantity > 0, "stock": quantity}
                store["total_stock"] += quantity
                store["available"] = store["available"] or quantity > 0
            price = shopify.money(variant.get("price"))
            compare_at = shopify.money(variant.get("compare_at_price"))
            webshop_sizes.append({
                "size": size,
                "in_stock": bool(variant.get("available")),
                "stock": None,
                "stock_known": False,
                "current_price": price,
                "list_price": compare_at if compare_at and price and compare_at > price else None,
                "source_size_variant_id": variant_id,
                "source_product_number": shopify.text(variant.get("sku")),
            })

        aarhus = stores[TRACKED_STORES[AARHUS_LOCATION]["slug"]]
        current_price, list_price = shopify.variant_prices(variants)
        kind = shopify.product_type(product)
        tags = shopify.strings(product.get("tags"))
        preloved = kind == "Preloved" or "preloved" in {tag.casefold() for tag in tags}
        colors = [tag.split(":", 1)[1].strip() for tag in tags if tag.casefold().startswith("color:")]
        color = " / ".join(colors) or None
        category, category_path = self._category(kind, tags)
        sku = next((shopify.text(variant.get("sku")) for variant in variants if shopify.text(variant.get("sku"))), None)
        canonical_url = self.storefront.canonical_url(handle)
        checked_at = datetime.now(timezone.utc).isoformat()
        return {
            "source_product_id": product_id,
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": sku,
            "name": name,
            "brand": shopify.text(product.get("vendor")),
            "product_type": kind,
            "condition": "preloved" if preloved else "new",
            "color": color,
            "color_group": shopify.color_group(colors[0]) if colors else None,
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": shopify.plain_text(product.get("body_html") or product.get("description")),
            "category": category,
            "category_path": category_path,
            "tags": tags,
            "images": shopify.images(product, BASE_URL),
            "webshop_sizes": webshop_sizes,
            "local_inventory": {"stores": stores},
            "local_total_stock": sum(store["total_stock"] for store in stores.values()),
            "local_available": any(store["available"] for store in stores.values()),
            "aarhus_total_stock": aarhus["total_stock"],
            "aarhus_available": aarhus["available"],
            "inventory_checked_at": checked_at,
            "raw": {
                "shopify_product_id": product_id,
                "shopify_handle": handle,
                "shopify_product_type": kind,
                "discovery_collections": product.get("_discovery_collections") or [],
                "delivery_tags": [tag for tag in tags if tag.casefold().startswith("delivery:")],
                "published_at": product.get("published_at"),
                "source_updated_at": product.get("updated_at"),
                "inventory_source": INVENTORY_URL,
            },
            "scraped_at": checked_at,
            "updated_at": checked_at,
        }

    @staticmethod
    def _size(product: dict[str, Any], variant: dict[str, Any]) -> Optional[str]:
        return (shopify.option_value(product, variant, ("size", "størrelse"))
                or shopify.text(variant.get("public_title") or variant.get("title")))

    @staticmethod
    def _stock_by_variant(product_id: str, payload: dict[str, Any], sizes: dict[str, str]) -> dict[str, dict[str, int]]:
        """Exact quantity per variant and tracked shop; incomplete data raises."""
        try:
            edges = payload["body"]["data"]["product"]["variants"]["edges"]
        except (KeyError, TypeError) as exc:
            raise SourcePayloadError(f"Takeoff inventory for product {product_id} has no variants") from exc
        stock: dict[str, dict[str, int]] = {}
        for edge in edges:
            node = (edge or {}).get("node") or {}
            variant_id = str(node.get("id") or "").rsplit("/", 1)[-1]
            if variant_id not in sizes:
                continue
            per_store: dict[str, int] = {}
            for store_edge in ((node.get("storeAvailability") or {}).get("edges") or []):
                store = (store_edge or {}).get("node") or {}
                location = shopify.text(((store.get("location") or {}).get("name") or "").replace("(Pick-up)", ""))
                if location not in TRACKED_STORES:
                    continue
                quantity = store.get("quantityAvailable")
                if type(quantity) is not int:
                    raise SourcePayloadError(f"Takeoff variant {variant_id} has no quantity for {location}")
                # Matches Takeoff's own drawer: a shop has the size only when
                # pickup is available and the quantity is positive.
                per_store[location] = max(quantity, 0) if store.get("available") else 0
            missing = sorted(set(TRACKED_STORES) - set(per_store))
            if missing:
                raise SourcePayloadError(f"Takeoff variant {variant_id} has no stock entry for {missing}")
            stock[variant_id] = per_store
        absent = sorted(set(sizes) - set(stock))
        if absent:
            raise SourcePayloadError(f"Takeoff inventory for product {product_id} omitted variants {absent[:5]}")
        return stock

    @staticmethod
    def _category(kind: Optional[str], tags: list[str]) -> tuple[Optional[str], list[str]]:
        lowered = {tag.casefold() for tag in tags}
        if kind == "Sneakers" or (kind == "Preloved" and "preloved-sneakers" in lowered):
            return "Sneakers", ["Footwear", "Sneakers"]
        if kind in CLOTHING_TYPES:
            return kind, ["Clothing", kind]
        if kind == "Preloved":
            return "Preloved clothing" if "preloved-clothing" in lowered else "Preloved", ["Preloved"]
        return kind, [kind] if kind else []
