"""Full-catalog importer for STOY men's clothing and public store availability."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://stoy.com/da"
# Both are canonical men's catalogue collections. Keep accessories out of the
# default set, but footwear is an intentional HIGHLIDE product category.
MEN_COLLECTION_HANDLES = ("all-clothing-for-men", "footwear-for-men")
TRACKED_STORES = {
    "stoy-aarhus": {"name": "STOY Aarhus", "address": "Store Torv 4, 8000 Aarhus C"},
    "stoy-copenhagen": {"name": "STOY København", "address": "Landemærket 8, 1119 København"},
}


class StoyScraper:
    """Read STOY's Shopify catalogue and server-rendered store availability."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/html,*/*",
        })

    def discover_products(self, collection_handles: Iterable[str] = MEN_COLLECTION_HANDLES) -> list[dict[str, Any]]:
        """Discover men's clothing and footwear, excluding accessory collections."""
        products: dict[int, dict[str, Any]] = {}
        collections_by_id: dict[int, set[str]] = {}
        for collection_handle in collection_handles:
            page = 1
            while True:
                response = self.session.get(
                    f"{BASE_URL}/collections/{collection_handle}/products.json",
                    params={"limit": 250, "page": page}, timeout=30,
                )
                response.raise_for_status()
                batch = response.json().get("products", [])
                if not batch:
                    break
                for product in batch:
                    if product.get("id") is not None:
                        product_id = int(product["id"])
                        products[product_id] = product
                        collections_by_id.setdefault(product_id, set()).add(collection_handle)
                if len(batch) < 250:
                    break
                page += 1
        for product_id, product in products.items():
            product["_discovery_collections"] = sorted(collections_by_id[product_id])
        return list(products.values())

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}.js", timeout=30)
        response.raise_for_status()
        return response.json()

    def fetch_product_page(self, url_or_handle: str) -> str:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}", timeout=30)
        response.raise_for_status()
        return response.text

    def product_to_row(self, product: dict[str, Any], page_html: str) -> dict[str, Any]:
        """Build one colour row; STOY publishes one colour per Shopify product."""
        product_id = int(product["id"])
        handle = product.get("handle") or self._handle_from_url(str(product_id))
        canonical_url = f"{BASE_URL}/products/{handle}"
        page_data, inventory, store_source = self._page_data(page_html)
        custom = (page_data.get("metafields") or {}).get("custom") or {}
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        style_code = self._text(custom.get("manufacturer_style_code"))
        color = self._color(custom, product, style_code)
        source_parent_id = self._style_reference(style_code, variants, product_id)
        specifications = self._specifications(custom)
        model_info, size_guide = self._size_and_fit(page_html)
        webshop_sizes = []
        for variant in variants:
            size = self._text(variant.get("public_title") or variant.get("title"))
            store_states = {
                slug: bool(inventory["stores"][slug]["sizes"].get(size, {}).get("available"))
                for slug in TRACKED_STORES
            }
            webshop_sizes.append({
                "size": size,
                "in_stock": bool(variant.get("available")),
                "stock": None,
                "stock_known": False,
                "local_available": any(store_states.values()),
                "local_stores": [slug for slug, available in store_states.items() if available],
                "source_size_variant_id": str(variant["id"]),
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })
        current_price = self._price(product.get("price"))
        list_price = self._price(product.get("compare_at_price"))
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None
        scraped_at = datetime.now(timezone.utc).isoformat()
        category_path = self._strings(custom.get("category_stoy"))
        collections = page_data.get("collections") or []
        return {
            "source_parent_id": source_parent_id,
            "source_color_id": str(product_id),
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": style_code,
            "name": product.get("title") or None,
            "brand": product.get("vendor") or None,
            "product_type": product.get("type") or None,
            "color": color,
            "color_group": self._text(custom.get("filter_color_group")),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": self._plain_text(product.get("description") or product.get("content") or ""),
            "specifications": specifications,
            "materials": self._materials(custom.get("composition")),
            "fit": self._text(custom.get("sizing")),
            "country_of_origin": self._text(custom.get("country")),
            "category": category_path[-1] if category_path else product.get("type") or None,
            "category_path": category_path,
            "collections": [self._text(item.get("title")) for item in collections if self._text(item.get("title"))],
            "tags": self._strings(product.get("tags")),
            "images": self._images(product),
            "model_info": model_info,
            "size_guide": size_guide,
            "webshop_sizes": webshop_sizes,
            "local_inventory": inventory,
            "local_total_stock": 0,
            "local_available": any(store["available"] for store in inventory["stores"].values()),
            "aarhus_total_stock": 0,
            "aarhus_available": inventory["stores"]["stoy-aarhus"]["available"],
            "inventory_checked_at": scraped_at,
            "raw": {
                "shopify_product_id": str(product_id),
                "shopify_handle": handle,
                "shopify_product_type": product.get("type"),
                "manufacturer_style_code": style_code,
                "color_swatch": custom.get("color_swatch"),
                "connected_products": custom.get("connected_products") or [],
                "discovery_collections": product.get("_discovery_collections") or [],
                "store_availability": store_source,
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _page_data(cls, html: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        node = soup.select_one("#stape-product-data")
        try:
            product_data = json.loads(node.string) if node and node.string else {}
        except json.JSONDecodeError:
            product_data = {}
        inventory = {"stores": {slug: cls._empty_store(store["name"]) for slug, store in TRACKED_STORES.items()}}
        raw: dict[str, Any] = {}
        for store_node in soup.select(".store-availability-drawer__store"):
            name = store_node.select_one(".store-availability-drawer__name")
            address = store_node.select_one(".store-availability-drawer__address")
            label = name.get_text(" ", strip=True) if name else ""
            slug = cls._store_slug(label)
            if not slug:
                continue
            store = inventory["stores"][slug]
            if address:
                store["address"] = address.get_text(" ", strip=True)
            sizes: dict[str, bool] = {}
            for size_node in store_node.select(".store-availability-drawer__size"):
                size_label = size_node.select_one(".store-availability-drawer__size-label")
                if size_label:
                    size = size_label.get_text(" ", strip=True)
                    available = "is-available" in (size_node.get("class") or [])
                    store["sizes"][size] = {"available": available, "stock": None}
                    sizes[size] = available
            store["available"] = any(sizes.values())
            raw[slug] = {"name": label, "address": store.get("address"), "sizes": sizes}
        return product_data, inventory, raw

    @classmethod
    def _specifications(cls, custom: dict[str, Any]) -> dict[str, Any]:
        """Keep STOY's real product metafields, not theme-specific accordions."""
        source_keys = (
            "base_category_stoy",
            "category_stoy",
            "sub_category_stoy",
            "composition",
            "country",
            "gender",
            "manufacturer_style_code",
            "sizing",
        )
        specifications: dict[str, Any] = {}
        for key in source_keys:
            value = custom.get(key)
            if isinstance(value, list):
                values = cls._strings(value)
                if values:
                    specifications[key] = values
            elif (text := cls._text(value)):
                specifications[key] = text
        return specifications

    @classmethod
    def _size_and_fit(cls, html: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Parse STOY's optional ``Størrelse & Pasform`` product accordion."""
        soup = BeautifulSoup(html, "html.parser")
        content = None
        for accordion in soup.select(".product__accordion"):
            summary = accordion.find("summary")
            title = summary.get_text(" ", strip=True).casefold() if summary else ""
            if "størrelse" in title and "pasform" in title:
                content = accordion.select_one(".accordion__content")
                break
        text = content.get_text("\n", strip=True) if content else ""
        lines = [line.strip(" •-\t") for line in text.splitlines() if line.strip(" •-\t")]
        sizing_note = soup.select_one(".product-sizing-note")
        if sizing_note:
            note = sizing_note.get_text(" ", strip=True)
            if note and note not in lines:
                lines.append(note)

        model_info: dict[str, Any] = {}
        model_line_index: int | None = None
        for index, line in enumerate(lines):
            match = re.search(
                r"modellen\s+er\s+(\d+)\s*cm(?:\s*/\s*([^\s]+))?\s+og\s+bærer\s+størrelse\s+(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                height_cm, height_imperial, wears_size = match.groups()
                model_info = {"height_cm": int(height_cm), "wears_size": wears_size.strip()}
                if height_imperial:
                    model_info["height_imperial"] = height_imperial.strip()
                model_line_index = index
                break
        fit_lines = [line for index, line in enumerate(lines) if index != model_line_index]
        return model_info, {"fit_guidance": fit_lines} if fit_lines else {}

    @staticmethod
    def _store_slug(name: str) -> str | None:
        normalized = name.casefold()
        if "aarhus" in normalized:
            return "stoy-aarhus"
        if "copenhagen" in normalized or "københavn" in normalized:
            return "stoy-copenhagen"
        return None

    @staticmethod
    def _style_reference(style_code: str | None, variants: list[dict[str, Any]], product_id: int) -> str:
        if style_code:
            return style_code.rsplit("-", 1)[0] or style_code
        sku = next((str(item.get("sku")) for item in variants if item.get("sku")), "")
        return sku.rsplit("-", 2)[0] or str(product_id)

    @classmethod
    def _color(cls, custom: dict[str, Any], product: dict[str, Any], style_code: str | None) -> str | None:
        swatch = custom.get("color_swatch") or {}
        if isinstance(swatch, dict) and cls._text(swatch.get("color_name")):
            return cls._text(swatch.get("color_name"))
        if style_code and "-" in style_code:
            return style_code.rsplit("-", 1)[-1]
        return None

    @staticmethod
    def _price(value: Any) -> float | None:
        return None if value is None or value == "" else float(value) / 100

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _strings(cls, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [text for value in values if (text := cls._text(value))]

    @classmethod
    def _materials(cls, composition: Any) -> list[str]:
        value = cls._text(composition) or ""
        return [part.strip() for part in re.split(r"[,;]", value) if part.strip()]

    @staticmethod
    def _plain_text(html: str) -> str | None:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return text or None

    @classmethod
    def _images(cls, product: dict[str, Any]) -> list[str]:
        images: list[str] = []
        for image in product.get("images", []):
            url = image.get("src") if isinstance(image, dict) else image
            if isinstance(url, str):
                absolute = urljoin(BASE_URL, url)
                if absolute not in images:
                    images.append(absolute)
        return images

    @staticmethod
    def _handle_from_url(url_or_handle: str) -> str:
        value = url_or_handle.split("?", 1)[0].rstrip("/")
        return value.rsplit("/products/", 1)[-1].rsplit("/", 1)[-1]

    @staticmethod
    def _empty_store(name: str) -> dict[str, Any]:
        return {"name": name, "stock_known": False, "available": False, "total_stock": None, "sizes": {}}
