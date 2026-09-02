"""Full-catalog importer for BESTSELLER Aarhus / Rømerhus men's products."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://bestseller-stores.dk"
NEWS_COLLECTION_HANDLE = "nyheder-test"
MEN_COLLECTION_HANDLES = (
    "overtoj-maend", "t-shirts-poloer", "skjorter-maend", "jeans-maend",
    "bukser-maend", "shorts-maend", "strik-cardigans-maend",
    "sweatshirts-maend", "jakkesaet-maend", NEWS_COLLECTION_HANDLE,
)
NEWS_ALLOWED_CATEGORY_PREFIXES = (
    "footwear_",
    "jeans_",
    "knit_",
    "outerwear_",
    "shirts_",
    "shorts_",
    "socks_",
    "sweatshirts_",
    "t-shirts & tops_",
    "tailoring_",
    "trousers_",
)
TRACKED_STORE_SLUGS = {
    61382557882: "romerhus-aarhus",
    111209972098: "gammeltorv-copenhagen",
}


class RomerhusScraper:
    """Read Shopify catalogue data and Rømerhus's exact Click & Collect stock."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/plain,*/*",
        })

    def discover_products(self, collection_handles: Iterable[str] = MEN_COLLECTION_HANDLES) -> list[dict[str, Any]]:
        """Return unique products from men's categories and eligible new arrivals."""
        by_id: dict[int, dict[str, Any]] = {}
        for handle in collection_handles:
            page = 1
            while True:
                response = self.session.get(
                    f"{BASE_URL}/collections/{handle}/products.json",
                    params={"limit": 250, "page": page}, timeout=30,
                )
                response.raise_for_status()
                batch = response.json().get("products", [])
                if not batch:
                    break
                for product in batch:
                    if handle == NEWS_COLLECTION_HANDLE and not self.is_news_catalog_product(product):
                        continue
                    if product.get("id") is not None:
                        by_id[int(product["id"])] = product
                if len(batch) < 250:
                    break
                page += 1
        return list(by_id.values())

    @staticmethod
    def is_news_catalog_product(product: dict[str, Any]) -> bool:
        """Keep men's clothing, socks, and footwear from the mixed news feed."""
        tags = {
            str(tag).strip().casefold()
            for tag in product.get("tags", [])
            if str(tag).strip()
        }
        if "news-mænd" not in tags:
            return False
        return any(
            tag.startswith(prefix)
            for tag in tags
            for prefix in NEWS_ALLOWED_CATEGORY_PREFIXES
        )

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}.js", timeout=30)
        response.raise_for_status()
        return response.json()

    def fetch_store_locations(self) -> list[dict[str, Any]]:
        response = self.session.get(f"{BASE_URL}/apps/bestseller-functions/locations", timeout=30)
        response.raise_for_status()
        locations = response.json()
        if not isinstance(locations, list):
            raise RuntimeError("Rømerhus locations endpoint returned an unexpected response.")
        return locations

    def fetch_variant_stock(self, variant_ids: Iterable[int], batch_size: int = 100) -> dict[int, dict[str, Any]]:
        """Fetch exact per-store stock in the storefront's supported batch format."""
        ids = [int(variant_id) for variant_id in variant_ids]
        result: dict[int, dict[str, Any]] = {}
        for start in range(0, len(ids), batch_size):
            response = self.session.post(
                f"{BASE_URL}/apps/bestseller-functions/variant-stock",
                json=ids[start : start + batch_size], timeout=30,
            )
            response.raise_for_status()
            records = response.json()
            if not isinstance(records, list):
                raise RuntimeError("Rømerhus variant-stock endpoint returned an unexpected response.")
            result.update({int(item["id"]): item for item in records if item.get("id") is not None})
        return result

    def build_catalog_rows(self, products: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        products = list(products)
        locations = {int(location["id"]): location for location in self.fetch_store_locations() if location.get("id") is not None}
        variants = [variant for product in products for variant in product.get("variants", []) if variant.get("id") is not None]
        stock_by_variant = self.fetch_variant_stock(int(variant["id"]) for variant in variants)
        stores = self._tracked_stores(locations)
        return [self.product_to_row(product, stock_by_variant, stores) for product in products]

    def product_to_row(self, product: dict[str, Any], stock_by_variant: dict[int, dict[str, Any]], stores: dict[int, dict[str, Any]]) -> dict[str, Any]:
        product_id = int(product["id"])
        description, fields = self._description_fields(product.get("description") or product.get("content") or "")
        inventory = {"stores": {store["slug"]: self._empty_store(store["name"]) for store in stores.values()}}
        webshop_sizes: list[dict[str, Any]] = []
        for variant in product.get("variants", []):
            if variant.get("id") is None:
                continue
            variant_id, size = int(variant["id"]), str(variant.get("public_title") or variant.get("title") or "").strip()
            per_store = {int(item["id"]): int(item.get("available") or 0) for item in stock_by_variant.get(variant_id, {}).get("locations", []) if item.get("id") is not None}
            webshop_sizes.append({
                "size": size, "in_stock": bool(variant.get("available")), "stock": None, "stock_known": False,
                "source_size_variant_id": str(variant_id), "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })
            for location_id, store in stores.items():
                count, summary = per_store.get(location_id, 0), inventory["stores"][store["slug"]]
                summary["total_stock"] += count
                summary["available"] = summary["available"] or count > 0
                summary["sizes"][size] = {"available": count > 0, "stock": count}

        handle = product.get("handle") or self._handle_from_url(product.get("url") or str(product_id))
        current_price = self._price(product.get("price_min", product.get("price")))
        list_price = self._price(product.get("compare_at_price_min") or product.get("compare_at_price"))
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None
        aarhus = inventory["stores"].get("romerhus-aarhus", self._empty_store("BESTSELLER Aarhus - Rømerhus"))
        scraped_at = datetime.now(timezone.utc).isoformat()
        style_reference = fields.get("style_reference") or self._style_reference(product)
        canonical_url = f"{BASE_URL}/products/{handle}"
        return {
            "source_parent_id": style_reference or str(product_id), "source_color_id": str(product_id),
            "source_url": canonical_url, "canonical_url": canonical_url, "source_product_number": style_reference,
            "name": product.get("title") or None, "brand": product.get("vendor") or None,
            "color": fields.get("color"), "color_group": None, "current_price": current_price, "list_price": list_price,
            "currency": "DKK", "description": description or None, "materials": fields.get("materials", []),
            "fit": fields.get("fit"), "category": product.get("type") or None,
            "images": self._images(product),
            "webshop_sizes": webshop_sizes, "local_inventory": inventory,
            "local_total_stock": sum(store["total_stock"] for store in inventory["stores"].values()),
            "local_available": any(store["available"] for store in inventory["stores"].values()),
            "aarhus_total_stock": aarhus["total_stock"], "aarhus_available": aarhus["available"],
            "raw": {
                "shopify_product_id": str(product_id), "shopify_handle": handle, "product_type": product.get("type"),
                "tags": product.get("tags", []), "published_at": product.get("published_at"), "created_at": product.get("created_at"),
                "store_locations": {str(location_id): {"slug": store["slug"], "name": store["name"], "address": store.get("address")} for location_id, store in stores.items()},
                "source_variant_stock": {str(variant["id"]): stock_by_variant.get(int(variant["id"]), {}) for variant in product.get("variants", []) if variant.get("id") is not None},
            },
            "scraped_at": scraped_at, "updated_at": scraped_at,
        }

    @staticmethod
    def _description_fields(html: str) -> tuple[str, dict[str, Any]]:
        lines = [line.strip(" -\t") for line in BeautifulSoup(html, "html.parser").get_text("\n").splitlines() if line.strip()]
        fields: dict[str, Any] = {"materials": []}
        section: str | None = None
        for line in lines:
            normalized = line.rstrip(":").strip().lower()
            if normalized in {"description", "material", "colour", "color", "care instructions", "style reference"}:
                section = normalized
            elif section == "material":
                fields["materials"].append(line)
            elif section in {"colour", "color"}:
                fields["color"] = line
            elif section == "style reference":
                match = re.search(r"\b\d{6,}\b", line)
                if match:
                    fields["style_reference"] = match.group(0)
            elif section == "description":
                match = re.search(r"\bfit\s*:\s*(.+)$", line, flags=re.IGNORECASE)
                if match:
                    fields["fit"] = match.group(1).strip()
        return "\n".join(lines), fields

    @staticmethod
    def _style_reference(product: dict[str, Any]) -> str | None:
        return next((str(tag) for tag in product.get("tags", []) if re.fullmatch(r"\d{6,}", str(tag).strip())), None)

    @staticmethod
    def _price(value: Any) -> float | None:
        return None if value is None or value == "" else float(value) / 100

    @staticmethod
    def _absolute_url(url: str) -> str:
        return urljoin(BASE_URL, url)

    @classmethod
    def _images(cls, product: dict[str, Any]) -> list[str]:
        """Normalise Shopify's collection-image objects and product .js URLs."""
        urls: list[str] = []
        for image in product.get("images", []):
            if isinstance(image, dict):
                image = image.get("src") or image.get("url")
            if isinstance(image, str) and image:
                url = cls._absolute_url(image)
                if url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _handle_from_url(url_or_handle: str) -> str:
        value = url_or_handle.split("?", 1)[0].rstrip("/")
        return value.rsplit("/products/", 1)[-1].rsplit("/", 1)[-1]

    @staticmethod
    def _empty_store(name: str) -> dict[str, Any]:
        return {"name": name, "stock_known": True, "available": False, "total_stock": 0, "sizes": {}}

    @staticmethod
    def _tracked_stores(locations: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
        stores = {
            location_id: {"slug": slug, "name": location["name"], "address": location.get("address")}
            for location_id, slug in TRACKED_STORE_SLUGS.items()
            if (location := locations.get(location_id)) and location.get("localPickupEnabled")
        }
        if not stores:
            raise RuntimeError("Neither expected Rømerhus Click & Collect store was returned by the storefront.")
        return stores
