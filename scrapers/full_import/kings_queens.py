"""Kings & Queens (kingsqueens.dk): men's clothing and shoes, Bruun's Galleri shop.

Each Shopify product is one colour; its variants are sizes. The Aarhus shop's
availability comes from Shopify's standard pickup-availability fragment, one
request per size, which is what the product page itself loads:

    GET /variants/<variant id>/?section_id=pickup-availability

It lists every Kings & Queens shop as ``li.pickup-availability-list__item``
with ``is-available`` or ``is-unavailable``, including sold-out sizes. It
never publishes quantities, so local stock is boolean (``stock_known: false``).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from scrapers import shopify
from scrapers.inventory import unique_sizes
from scrapers.source_http import SourcePayloadError

BASE_URL = "https://kingsqueens.dk"
MEN_COLLECTION = "herre"
EXCLUDED_TYPES = {"accessories til herre", "halstørklæder til herre", "gift card", "gavekort"}
FOOTWEAR_TYPES = {"sko til herre", "sandaler til herre", "sneakers til herre", "støvler til herre", "loafer til herre"}
AARHUS_LOCATION = "K&Q Århus Bruuns"
AARHUS_SLUG = "kingsqueens-aarhus"
AARHUS_STORE = {"name": "Kings & Queens Aarhus, Bruun's Galleri", "address": "M. P. Bruuns Gade 25, 8000 Aarhus C"}


class KingsQueensScraper:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.storefront = shopify.ShopifyStorefront(BASE_URL, session)
        self.session = self.storefront.session

    # Discovery and fetching -------------------------------------------------

    def discover_products(self, collection: str = MEN_COLLECTION) -> list[dict[str, Any]]:
        return self.storefront.collection_products(collection, keep=self.is_catalog_product)

    @staticmethod
    def is_catalog_product(product: dict[str, Any]) -> bool:
        kind = (shopify.product_type(product) or "").casefold()
        handle = str(product.get("handle") or "")
        return kind not in EXCLUDED_TYPES and "gift-card" not in handle and "gavekort" not in handle

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        return self.storefront.product_js(url_or_handle)

    def fetch_aarhus_availability(
        self,
        product: dict[str, Any],
        *,
        variant_delay: float = 0.3,
    ) -> dict[str, bool]:
        """Aarhus pickup availability per variant id."""
        result = {}
        for index, variant in enumerate(product.get("variants") or []):
            if variant.get("id") is None:
                continue
            if index and variant_delay > 0:
                time.sleep(variant_delay)
            html = self.storefront.variant_section(variant["id"], "pickup-availability")
            result[str(variant["id"])] = self.aarhus_available_from_fragment(html)
        return result

    @staticmethod
    def aarhus_available_from_fragment(html: str) -> bool:
        soup = BeautifulSoup(html, "html.parser")
        items = soup.select("li.pickup-availability-list__item")
        if not items:
            raise SourcePayloadError("Kings & Queens pickup fragment lists no shops; retaining previous stock")
        for item in items:
            name = shopify.text(item.select_one(".pickup-availability-list__location").get_text(" ", strip=True)
                                if item.select_one(".pickup-availability-list__location") else None)
            if name != AARHUS_LOCATION:
                continue
            classes = set(item.get("class") or [])
            if classes >= {"is-available"} and "is-unavailable" not in classes:
                return True
            if "is-unavailable" in classes and "is-available" not in classes:
                return False
            raise SourcePayloadError(f"Kings & Queens Aarhus entry has no availability marker: {sorted(classes)}")
        raise SourcePayloadError(f"Kings & Queens pickup fragment does not list {AARHUS_LOCATION!r}")

    def close(self) -> None:
        self.storefront.close()

    # Row building ------------------------------------------------------------

    def product_to_row(self, product: dict[str, Any], aarhus_by_variant: dict[str, bool]) -> dict[str, Any]:
        product_id = str(product["id"])
        handle = shopify.text(product.get("handle"))
        name = shopify.text(product.get("title"))
        if not handle or not name:
            raise SourcePayloadError(f"Kings & Queens product {product_id} has no handle or title")
        variants = [variant for variant in product.get("variants") or [] if variant.get("id") is not None]
        if not variants:
            raise SourcePayloadError(f"Kings & Queens product {product_id} has no variants")
        sizes = unique_sizes(variants, lambda variant: self._size(product, variant), "Kings & Queens product")
        missing = [variant_id for variant_id in sizes if variant_id not in aarhus_by_variant]
        if missing:
            raise SourcePayloadError(f"Kings & Queens product {product_id} has no Aarhus availability for {missing[:5]}")

        aarhus_sizes = {}
        webshop_sizes = []
        for variant in variants:
            variant_id = str(variant["id"])
            size = sizes[variant_id]
            aarhus_sizes[size] = {"available": aarhus_by_variant[variant_id], "stock": None}
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
                "source_product_number": shopify.text(variant.get("sku")) or shopify.text(variant.get("barcode")),
            })
        aarhus_available = any(size["available"] for size in aarhus_sizes.values())
        store = {**AARHUS_STORE, "stock_known": False, "available": aarhus_available,
                 "total_stock": None, "sizes": aarhus_sizes}

        kind = shopify.product_type(product)
        color = shopify.option_value(product, variants[0], ("color", "colour", "farve"))
        current_price, list_price = shopify.variant_prices(variants)
        canonical_url = self.storefront.canonical_url(handle)
        checked_at = datetime.now(timezone.utc).isoformat()
        footwear = (kind or "").casefold() in FOOTWEAR_TYPES
        return {
            "source_product_id": product_id,
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": self._style_number(variants),
            "name": name,
            "brand": shopify.text(product.get("vendor")),
            "product_type": kind,
            "color": color,
            "color_group": shopify.color_group(color),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": shopify.plain_text(product.get("body_html") or product.get("description")),
            "category": kind,
            "category_path": (["Footwear", kind] if footwear else ["Clothing", kind]) if kind else ["Clothing"],
            "tags": shopify.strings(product.get("tags")),
            "images": shopify.images(product, BASE_URL),
            "webshop_sizes": webshop_sizes,
            "local_inventory": {"stores": {AARHUS_SLUG: store}},
            "local_total_stock": None,
            "local_available": aarhus_available,
            "aarhus_total_stock": None,
            "aarhus_available": aarhus_available,
            "inventory_checked_at": checked_at,
            "raw": {
                "shopify_product_id": product_id,
                "shopify_handle": handle,
                "shopify_product_type": kind,
                "discovery_collections": product.get("_discovery_collections") or [],
                "published_at": product.get("published_at"),
                "source_updated_at": product.get("updated_at"),
                "inventory_source": "pickup-availability section (boolean, no quantities)",
            },
            "scraped_at": checked_at,
            "updated_at": checked_at,
        }

    @staticmethod
    def _size(product: dict[str, Any], variant: dict[str, Any]) -> Optional[str]:
        return (shopify.option_value(product, variant, ("size", "størrelse"))
                or shopify.text(variant.get("public_title") or variant.get("title")))

    @staticmethod
    def _style_number(variants: list[dict[str, Any]]) -> Optional[str]:
        """SKU without the size suffix, e.g. 130_DK0A8CCKBLK1_BLACK_S -> 130_DK0A8CCKBLK1_BLACK."""
        prefixes = {sku.rsplit("_", 1)[0] for variant in variants
                    if (sku := shopify.text(variant.get("sku"))) and "_" in sku}
        return prefixes.pop() if len(prefixes) == 1 else None
