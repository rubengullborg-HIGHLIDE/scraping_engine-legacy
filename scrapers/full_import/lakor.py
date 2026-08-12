"""Full-catalog importer for LAKOR clothing and its public store availability."""

from __future__ import annotations

import colorsys
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.lakor.dk"
CLOTHING_COLLECTION_HANDLE = "all-clothing"
CLOTHING_PRODUCT_TYPES = frozenset({
    "T-Shirt", "T-shirt", "Sweatshirt", "Shirt", "Short Sleeve Shirt",
    "Overshirt", "Pants", "Knit", "Jacket", "Shorts",
})
TRACKED_STORES = {
    "lakor-aarhus": {"name": "LAKOR Shop Aarhus", "path": "/pages/lakor-shop-aarhus"},
    "lakor-copenhagen": {"name": "LAKOR Shop KBH", "path": "/pages/lakor-shop-copenhagen"},
    "lakor-aalborg": {"name": "LAKOR Shop Aalborg", "path": "/pages/lakor-shop-aalborg"},
}


class LakorScraper:
    """Read LAKOR's Shopify catalogue and variant-level store availability."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/plain,*/*",
        })

    def discover_products(self, collection_handle: str = CLOTHING_COLLECTION_HANDLE) -> list[dict[str, Any]]:
        """Discover product candidates from LAKOR's clothing collection.

        Shopify's collection feed deliberately omits product type, so callers
        must verify the clothing type from each hydrated product feed.
        """
        by_id: dict[int, dict[str, Any]] = {}
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
                    by_id[int(product["id"])] = product
            if len(batch) < 250:
                break
            page += 1
        return list(by_id.values())

    @staticmethod
    def is_clothing(product: dict[str, Any]) -> bool:
        return product.get("type") in CLOTHING_PRODUCT_TYPES

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

    def fetch_variant_store_availability(self, variant_id: int) -> dict[str, Any]:
        """Return LAKOR's availability notice for one size variant.

        LAKOR's generic ``i vores butikker`` wording denotes availability at all
        of its tracked stores. When a size is unavailable at one or more shops,
        the endpoint instead names the remaining shops explicitly. The endpoint
        never exposes quantities.
        """
        response = self.session.get(
            f"{BASE_URL}/variants/{variant_id}",
            params={"section_id": "store-availability"}, timeout=30,
        )
        response.raise_for_status()
        return self._store_availability_from_html(response.text)

    @staticmethod
    def _store_availability_from_html(html: str) -> dict[str, Any]:
        """Parse LAKOR's store availability fragment without inferring quantities."""
        soup = BeautifulSoup(html, "html.parser")
        notice = soup.select_one(".availability-text")
        text = notice.get_text(" ", strip=True) if notice else ""
        available = bool(re.search(r"\b(på lager|få tilbage|kun tilbage)\b", text, flags=re.IGNORECASE)) and not bool(
            re.search(r"ikke\s+på\s+lager", text, flags=re.IGNORECASE)
        )
        low_stock = bool(re.search(r"\b(få tilbage|kun\s+få tilbage)\b", text, flags=re.IGNORECASE))
        stores: set[str] = set()
        if notice:
            for link in notice.select("a[href]"):
                href = link.get("href", "").split("?", 1)[0]
                for slug, store in TRACKED_STORES.items():
                    if href == store["path"]:
                        stores.add(slug)
        all_tracked_stores = bool(re.search(r"\bi\s+vores\s+butikker\b", text, flags=re.IGNORECASE))
        if available and all_tracked_stores:
            stores = set(TRACKED_STORES)
            store_scope = "all_tracked_stores"
        elif available and stores:
            store_scope = "named_stores"
        else:
            store_scope = "none"
        return {
            "available": available,
            "low_stock": low_stock if available else False,
            "stores": sorted(stores),
            "store_scope": store_scope,
            "notice": text,
        }

    def build_catalog_rows(
        self,
        products: Iterable[tuple[dict[str, Any], str]],
        *,
        variant_delay: float = 0.2,
        no_delay: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            self.product_to_row(product, page_html, variant_delay=variant_delay, no_delay=no_delay)
            for product, page_html in products
        ]

    def product_to_row(
        self,
        product: dict[str, Any],
        page_html: str,
        *,
        variant_delay: float = 0.2,
        no_delay: bool = False,
    ) -> dict[str, Any]:
        product_id = int(product["id"])
        details = self._page_details(page_html)
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        store_availability: dict[int, dict[str, Any]] = {}
        for index, variant in enumerate(variants):
            if index and not no_delay:
                time.sleep(variant_delay)
            variant_id = int(variant["id"])
            store_availability[variant_id] = self.fetch_variant_store_availability(variant_id)

        inventory = {"stores": {slug: self._empty_store(store["name"]) for slug, store in TRACKED_STORES.items()}}
        webshop_sizes: list[dict[str, Any]] = []
        local_available = False
        for variant in variants:
            variant_id = int(variant["id"])
            size = str(variant.get("public_title") or variant.get("title") or "").strip()
            availability = store_availability[variant_id]
            webshop_sizes.append({
                "size": size,
                "in_stock": bool(variant.get("available")),
                "stock": None,
                "stock_known": False,
                "local_available": availability["available"],
                "local_low_stock": availability["low_stock"],
                "local_stores": availability["stores"],
                "local_store_scope": availability["store_scope"],
                "source_size_variant_id": str(variant_id),
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })
            local_available = local_available or availability["available"]
            if availability["available"]:
                for slug in availability["stores"]:
                    store = inventory["stores"][slug]
                    store["available"] = True
                    store["sizes"][size] = {
                        "available": True,
                        "stock": None,
                        "low_stock": availability["low_stock"],
                    }

        handle = product.get("handle") or self._handle_from_url(str(product_id))
        current_price = self._price(product.get("price_min", product.get("price")))
        list_price = self._price(product.get("compare_at_price_min") or product.get("compare_at_price"))
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None
        sku_parts = self._sku_parts(variants)
        color = details["specifications"].get("color") or sku_parts.get("color") or self._color_from_tags(product)
        style_reference = sku_parts.get("style_reference") or str(product_id)
        aarhus = inventory["stores"]["lakor-aarhus"]
        scraped_at = datetime.now(timezone.utc).isoformat()
        canonical_url = f"{BASE_URL}/products/{handle}"
        return {
            "source_parent_id": style_reference,
            "source_color_id": str(product_id),
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": style_reference,
            "name": product.get("title") or None,
            "brand": product.get("vendor") or None,
            "color": color,
            "color_group": self._color_group(details.get("color_swatch_hex")),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": details["description"] or self._plain_text(product.get("description") or product.get("content") or ""),
            "highlights": details["highlights"],
            "specifications": details["specifications"],
            "materials": self._materials(details["specifications"].get("materials"), product.get("tags", [])),
            "fit": details["specifications"].get("fit") or details["size_guide"].get("fit"),
            "care_instructions": details["specifications"].get("care_instructions"),
            "country_of_origin": details["specifications"].get("country_of_origin"),
            "category": product.get("type") or None,
            "images": self._images(product),
            "model_info": details["model_info"],
            "size_guide": details["size_guide"],
            "webshop_sizes": webshop_sizes,
            "local_inventory": inventory,
            "local_available": local_available,
            "aarhus_available": True if aarhus["available"] else None,
            "raw": {
                "shopify_product_id": str(product_id),
                "shopify_handle": handle,
                "product_type": product.get("type"),
                "tags": product.get("tags", []),
                "color_swatch_hex": details.get("color_swatch_hex"),
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
                "variant_store_availability": {str(key): value for key, value in store_availability.items()},
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _page_details(cls, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        specifications = cls._specifications(cls._section_text(soup, "collapsible-specifications"))
        size_guide = cls._size_guide(soup)
        return {
            "description": cls._section_text(soup, "collapsible-description"),
            "highlights": cls._highlights(cls._section_text(soup, "collapsible-highlights")),
            "specifications": specifications,
            "size_guide": size_guide,
            "model_info": cls._model_info(soup),
            "color_swatch_hex": cls._active_color_swatch_hex(soup),
        }

    @staticmethod
    def color_handles(page_html: str) -> list[str]:
        """Return every product page linked by the current product's colour swatches."""
        soup = BeautifulSoup(page_html, "html.parser")
        handles: list[str] = []
        for link in soup.select('.color-swatch-list a[href*="/products/"]'):
            href = link.get("href")
            if not href:
                continue
            handle = LakorScraper._handle_from_url(href)
            if handle not in handles:
                handles.append(handle)
        return handles

    @staticmethod
    def _active_color_swatch_hex(soup: BeautifulSoup) -> str | None:
        active = soup.select_one(".color-swatch__item.active[style]")
        if not active:
            return None
        match = re.search(r"background-color\s*:\s*(#[0-9a-f]{6})", active.get("style", ""), flags=re.IGNORECASE)
        return match.group(1).upper() if match else None

    @staticmethod
    def _color_group(hex_color: str | None) -> str | None:
        """Map LAKOR's displayed swatch colour to a stable broad colour family."""
        if not hex_color or not re.fullmatch(r"#[0-9A-Fa-f]{6}", hex_color):
            return None
        red, green, blue = (int(hex_color[index : index + 2], 16) / 255 for index in (1, 3, 5))
        hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
        if value <= 0.16:
            return "Sort"
        if saturation <= 0.14:
            if value >= 0.9:
                return "Hvid"
            if value >= 0.67:
                return "Grå"
            return "Sort"
        degrees = hue * 360
        if degrees < 15 or degrees >= 345:
            return "Rød"
        if degrees < 42:
            return "Orange"
        if degrees < 68:
            return "Gul"
        if degrees < 165:
            return "Grøn"
        if degrees < 260:
            return "Blå"
        if degrees < 305:
            return "Lilla"
        return "Pink"

    @staticmethod
    def _section_text(soup: BeautifulSoup, section_id: str) -> str | None:
        section = soup.find(id=section_id)
        if not section:
            return None
        for heading in section.find_all(["h3", "h4"]):
            heading.decompose()
        text = section.get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _highlights(text: str | None) -> list[str]:
        if not text:
            return []
        return [line.strip("• -\t ") for line in text.splitlines() if line.strip("• -\t ")]

    @staticmethod
    def _specifications(text: str | None) -> dict[str, str]:
        if not text:
            return {}
        labels = {
            "farve": "color",
            "colour": "color",
            "materiale": "materials",
            "pasform": "fit",
            "vask": "care_instructions",
            "produceret i": "country_of_origin",
        }
        result: dict[str, str] = {}
        pending_key: str | None = None
        for line in text.splitlines():
            stripped = line.strip("• -\t ")
            match = re.match(r"^([^:]+):\s*(.*)$", stripped, flags=re.IGNORECASE)
            if not match:
                produced_in = re.match(r"^produceret i\s*(.*)$", stripped, flags=re.IGNORECASE)
                if produced_in:
                    value = produced_in.group(1).strip()
                    if value:
                        result["country_of_origin"] = value
                        pending_key = None
                    else:
                        pending_key = "country_of_origin"
                elif pending_key and stripped:
                    result[pending_key] = stripped
                    pending_key = None
                continue
            label, value = match.groups()
            key = labels.get(label.strip().lower())
            if key:
                if value.strip():
                    result[key] = value.strip()
                    pending_key = None
                else:
                    pending_key = key
        return result

    @classmethod
    def _size_guide(cls, soup: BeautifulSoup) -> dict[str, Any]:
        drawer = soup.select_one('drawer-content[id$="-size-chart-drawer"]')
        if not drawer:
            return {}
        content = drawer.select_one(".drawer__content .rte")
        if not content:
            return {}
        paragraphs = [paragraph.get_text(" ", strip=True) for paragraph in content.find_all("p")]
        result: dict[str, Any] = {}
        if paragraphs:
            match = re.search(r"\ber\s+(.+?\bfit)\b", paragraphs[0], flags=re.IGNORECASE)
            if match:
                result["fit"] = match.group(1).strip()
            if len(paragraphs) > 1:
                result["fit_guidance"] = paragraphs[1]
        image = content.find("img")
        if image and image.get("src"):
            result["measurement_image"] = cls._absolute_url(image["src"])
        columns = []
        for column in content.select(".dimensions > .dimension"):
            values = [value.get_text(" ", strip=True) for value in column.find_all("div", recursive=False)]
            if values:
                columns.append(values)
        if columns and len(columns) > 1 and len(columns[0]) > 1:
            sizes = columns[0][1:]
            measurements: dict[str, dict[str, str]] = {size: {} for size in sizes}
            for column in columns[1:]:
                if not column:
                    continue
                label = column[0]
                for size, value in zip(sizes, column[1:]):
                    measurements[size][label] = value
            result["measurements"] = measurements
        return result

    @staticmethod
    def _model_info(soup: BeautifulSoup) -> dict[str, str]:
        text = soup.get_text(" ", strip=True)
        match = re.search(r"([A-ZÆØÅ][\wÆØÅæøå-]+)\s+er\s+(\d+)\s*cm\s+og\s+bærer\s+størrelse\s+([\w-]+)", text, flags=re.IGNORECASE)
        if not match:
            return {}
        name, height_cm, size = match.groups()
        return {"name": name, "height_cm": int(height_cm), "wears_size": size}

    @staticmethod
    def _sku_parts(variants: Iterable[dict[str, Any]]) -> dict[str, str]:
        for variant in variants:
            sku = str(variant.get("sku") or "")
            parts = [part.strip() for part in sku.split("\\")]
            if len(parts) >= 2 and parts[0] and parts[1]:
                return {"style_reference": parts[0], "color": parts[1]}
        return {}

    @staticmethod
    def _color_from_tags(product: dict[str, Any]) -> str | None:
        excluded = {"clothing", "lakor", "unisex", "discounted", str(product.get("type", "")).lower()}
        for tag in product.get("tags", []):
            value = str(tag).strip()
            if not value or value.lower() in excluded or re.match(r"^\d+%\s", value) or re.match(r"^\d{2}\s", value):
                continue
            return value
        return None

    @staticmethod
    def _materials(value: str | None, tags: Iterable[Any]) -> list[str]:
        source = value or next((str(tag) for tag in tags if re.match(r"^\d+%\s", str(tag).strip())), "")
        return [part.strip() for part in source.split(",") if part.strip()]

    @staticmethod
    def _price(value: Any) -> float | None:
        return None if value is None or value == "" else float(value) / 100

    @staticmethod
    def _plain_text(html: str) -> str | None:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _absolute_url(url: str) -> str:
        return urljoin(BASE_URL, url)

    @classmethod
    def _images(cls, product: dict[str, Any]) -> list[str]:
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
        return {"name": name, "stock_known": False, "available": False, "total_stock": None, "sizes": {}}
