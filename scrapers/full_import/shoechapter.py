"""Full-catalog importer for Shoe Chapter men's footwear and Aarhus stock."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag


BASE_URL = "https://shoechapter.com"
MEN_COLLECTION_HANDLE = "men"
FOOTWEAR_PRODUCT_TYPES = frozenset({"Sneakers"})
TRACKED_STORES = {
    "shoechapter-aarhus": {
        "name": "Shoe Chapter Aarhus",
        "address": "Store Torv 6, 8000 Aarhus",
    },
}


class ShoeChapterScraper:
    """Read Shoe Chapter's Shopify catalogue and rendered Aarhus inventory."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/html,*/*",
        })

    def discover_products(self, collection_handle: str = MEN_COLLECTION_HANDLE) -> list[dict[str, Any]]:
        """Discover men's footwear, excluding socks, magazines, and lifestyle items."""
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
                if product.get("id") is not None and self.is_footwear(product):
                    by_id[int(product["id"])] = product
            if len(batch) < 250:
                break
            page += 1
        return list(by_id.values())

    @staticmethod
    def is_footwear(product: dict[str, Any]) -> bool:
        return (product.get("type") or product.get("product_type")) in FOOTWEAR_PRODUCT_TYPES

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
        """Build one colour row; Shoe Chapter publishes one colour per product."""
        product_id = int(product["id"])
        handle = product.get("handle") or self._handle_from_url(str(product_id))
        canonical_url = f"{BASE_URL}/products/{handle}"
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        details = self._page_details(page_html)
        inventory, local_by_variant, store_source = self._inventory_from_page(page_html, variants)
        style_reference = self._style_reference(variants, product_id)
        color = self._color(product)
        materials, material_sources = self._materials(details["highlights"])
        fit = self._fit(details["highlights"])
        country = self._country(details["highlights"])
        webshop_sizes = []
        for variant in variants:
            variant_id = str(variant["id"])
            size = self._text(variant.get("public_title") or variant.get("title"))
            local = local_by_variant.get(variant_id, {})
            webshop_sizes.append({
                "size": size,
                "in_stock": bool(variant.get("available")),
                "stock": None,
                "stock_known": False,
                "local_available": bool(local.get("available")),
                "local_stores": ["shoechapter-aarhus"] if local.get("available") else [],
                "local_stock": local.get("stock"),
                "source_size_variant_id": variant_id,
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })
        current_price = self._price(product.get("price"))
        list_price = self._price(product.get("compare_at_price"))
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None
        store = inventory["stores"]["shoechapter-aarhus"]
        local_total_stock = self._known_stock_total(inventory)
        aarhus_total_stock = store["total_stock"] if store.get("stock_known") and isinstance(store.get("total_stock"), int) else None
        scraped_at = datetime.now(timezone.utc).isoformat()
        return {
            "source_parent_id": style_reference,
            "source_color_id": str(product_id),
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": style_reference,
            "name": product.get("title") or None,
            "brand": product.get("vendor") or None,
            "product_type": product.get("type") or None,
            "color": color,
            "color_group": self._color_group(color),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": details["description"] or self._plain_text(product.get("description") or product.get("content") or ""),
            "highlights": details["highlights"],
            "specifications": self._specifications(
                product,
                material_sources=material_sources,
                fit_source=fit,
                country_source=country,
            ),
            "materials": materials,
            "fit": fit,
            "country_of_origin": country,
            "category": product.get("type") or None,
            "category_path": [product.get("type")] if product.get("type") else [],
            "tags": self._strings(product.get("tags")),
            "images": self._images(product),
            "size_guide": details["size_guide"],
            "webshop_sizes": webshop_sizes,
            "local_inventory": inventory,
            "local_total_stock": local_total_stock,
            "local_available": bool(store.get("available")),
            "aarhus_total_stock": aarhus_total_stock,
            "aarhus_available": bool(store.get("available")),
            "inventory_checked_at": scraped_at,
            "raw": {
                "shopify_product_id": str(product_id),
                "shopify_handle": handle,
                "shopify_product_type": product.get("type"),
                "tags": product.get("tags", []),
                "color_handles": self.color_handles(page_html),
                "store_inventory": store_source,
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _page_details(cls, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        description_node = cls._accordion_content(soup, "Beskrivelse")
        size_guide_node = soup.select_one(".sizeguide_content")
        return {
            "description": cls._description(description_node),
            "highlights": cls._highlights(description_node),
            "size_guide": cls._size_guide(size_guide_node),
        }

    @staticmethod
    def color_handles(page_html: str) -> list[str]:
        soup = BeautifulSoup(page_html, "html.parser")
        handles: list[str] = []
        for link in soup.select('.product-single__colors a[href*="/products/"]'):
            href = link.get("href")
            if not href:
                continue
            handle = ShoeChapterScraper._handle_from_url(href)
            if handle not in handles:
                handles.append(handle)
        return handles

    @classmethod
    def _inventory_from_page(
        cls,
        html: str,
        variants: Iterable[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
        soup = BeautifulSoup(html, "html.parser")
        by_variant: dict[str, dict[str, Any]] = {}
        store = cls._empty_store(TRACKED_STORES["shoechapter-aarhus"]["name"])
        store["address"] = TRACKED_STORES["shoechapter-aarhus"]["address"]
        store["stock_known"] = True
        store["total_stock"] = 0
        size_by_variant = {
            str(variant["id"]): cls._text(variant.get("public_title") or variant.get("title"))
            for variant in variants
            if variant.get("id") is not None
        }
        raw: dict[str, Any] = {}
        spans = soup.select("variant-inventory [data-variant-id]")
        if not spans:
            store["stock_known"] = False
            store["total_stock"] = None
            return {"stores": {"shoechapter-aarhus": store}}, by_variant, raw
        for span in spans:
            variant_id = str(span.get("data-variant-id") or "")
            text = span.get_text(" ", strip=True)
            stock = cls._stock_from_text(text)
            available = bool(stock and stock > 0)
            if stock is None:
                available = cls._available_from_text(text, span.get("data-status"))
                if available:
                    store["stock_known"] = False
                    store["total_stock"] = None
            size = size_by_variant.get(variant_id)
            by_variant[variant_id] = {"available": available, "stock": stock}
            raw[variant_id] = {"text": text, "status": span.get("data-status"), "stock": stock}
            if not size:
                continue
            store["sizes"][size] = {"available": available, "stock": stock}
            store["available"] = store["available"] or available
            if isinstance(stock, int) and isinstance(store.get("total_stock"), int):
                store["total_stock"] += stock
        return {"stores": {"shoechapter-aarhus": store}}, by_variant, raw

    @staticmethod
    def _known_stock_total(inventory: dict[str, Any]) -> int:
        total = 0
        for store in (inventory.get("stores") or {}).values():
            for size in (store.get("sizes") or {}).values():
                stock = size.get("stock")
                if isinstance(stock, int):
                    total += stock
        return total

    @staticmethod
    def _stock_from_text(text: str) -> int | None:
        match = re.search(r"kun\s+(\d+)\s+enhed", text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
        match = re.search(r"\b(\d+)\s+enheder?\b", text, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _available_from_text(text: str, status: str | None) -> bool:
        if status == "error" or re.search(r"ikke\s+på\s+lager|udsolgt", text, flags=re.IGNORECASE):
            return False
        return bool(re.search(r"på\s+lager|tilbage|lagerstatus", text, flags=re.IGNORECASE))

    @staticmethod
    def _accordion_content(soup: BeautifulSoup, title: str) -> Tag | None:
        for details in soup.select("details.accordion"):
            summary = details.find("summary")
            if summary and title.casefold() in summary.get_text(" ", strip=True).casefold():
                return details.select_one(".accordion__content")
        return None

    @staticmethod
    def _description(node: Tag | None) -> str | None:
        if not node:
            return None
        paragraphs = [item.get_text(" ", strip=True) for item in node.find_all("p")]
        text = "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
        return text or None

    @staticmethod
    def _highlights(node: Tag | None) -> list[str]:
        if not node:
            return []
        return [item.get_text(" ", strip=True) for item in node.select("li") if item.get_text(" ", strip=True)]

    @classmethod
    def _size_guide(cls, node: Tag | None) -> dict[str, Any]:
        if not node:
            return {}
        rows: list[dict[str, str]] = []
        title = None
        for table in node.find_all("table"):
            headers: list[str] = []
            for tr in table.find_all("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
                cells = [cell for cell in cells if cell]
                if not cells:
                    continue
                if len(cells) == 1 and not headers:
                    title = cells[0]
                    continue
                if not headers and any(cell.casefold() in {"eur", "eu", "us", "uk", "jp (cm)", "cm"} for cell in cells):
                    headers = cells
                    continue
                if headers and len(cells) == len(headers):
                    rows.append(dict(zip(headers, cells)))
        return {"title": title, "rows": rows} if rows else {}

    @classmethod
    def _specifications(
        cls,
        product: dict[str, Any],
        *,
        material_sources: list[str],
        fit_source: str | None,
        country_source: str | None,
    ) -> dict[str, Any]:
        specifications: dict[str, Any] = {
            "product_type": product.get("type"),
            "tags": cls._strings(product.get("tags")),
        }
        if material_sources:
            specifications["material_sources"] = material_sources
        if fit_source:
            specifications["fit_source"] = fit_source
        if country_source:
            specifications["country_source"] = country_source
        return {key: value for key, value in specifications.items() if value}

    @staticmethod
    def _materials(highlights: list[str]) -> tuple[list[str], list[str]]:
        materials: list[str] = []
        sources: list[str] = []
        material_words = (
            "cordura",
            "gummi",
            "læder",
            "laeder",
            "mesh",
            "nubuck",
            "ruskind",
            "skind",
            "skum",
            "skumsål",
            "suede",
            "vegansk",
        )
        component_words = (
            "for",
            "indersål",
            "mellemsål",
            "overdel",
            "skumsål",
            "snørebånd",
            "sål",
            "tåkap",
            "ydersål",
        )
        for line in highlights:
            normalized = line.casefold()
            has_material = any(word in normalized for word in material_words)
            has_component = any(word in normalized for word in component_words)
            if has_material and has_component:
                sources.append(line)
                materials.append(line)
        return materials, sources

    @staticmethod
    def _fit(highlights: list[str]) -> str | None:
        return next((line for line in highlights if "størrelsen" in line.casefold()), None)

    @staticmethod
    def _country(highlights: list[str]) -> str | None:
        for line in highlights:
            match = re.search(r"(?:produceret|håndproduceret)\s+i\s+(.+)$", line, flags=re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    @staticmethod
    def _style_reference(variants: list[dict[str, Any]], product_id: int) -> str:
        for variant in variants:
            sku = str(variant.get("sku") or "").strip()
            size = str(variant.get("public_title") or variant.get("title") or "").strip()
            if not sku:
                continue
            sku_size = re.sub(r"[^0-9A-Za-z]+", "-", size).strip("-")
            if sku_size and sku.casefold().endswith(f"-{sku_size}".casefold()):
                return sku[: -(len(sku_size) + 1)]
            return sku.rsplit("-", 1)[0] or sku
        return str(product_id)

    @staticmethod
    def _color(product: dict[str, Any]) -> str | None:
        title = str(product.get("title") or "")
        return title.rsplit(" - ", 1)[-1].strip() if " - " in title else None

    @staticmethod
    def _color_group(color: str | None) -> str | None:
        if not color:
            return None
        tokens = re.split(r"[^a-zA-Z]+", color.casefold())
        groups = (
            ("Black", {"black", "sort"}),
            ("White", {"white", "hvid", "cream", "ivory", "off", "chalk"}),
            ("Grey", {"grey", "gray", "silver", "raincloud", "castlerock", "slate"}),
            ("Blue", {"blue", "navy", "indigo"}),
            ("Green", {"green", "olive", "moss", "wakame"}),
            ("Brown", {"brown", "taupe", "oak", "tan", "cork", "earth", "beige"}),
            ("Red", {"red", "burgundy", "grenadine"}),
            ("Yellow", {"yellow", "gold", "ochre", "banana"}),
            ("Orange", {"orange"}),
            ("Purple", {"purple", "violet", "grape"}),
            ("Pink", {"pink"}),
        )
        for group, values in groups:
            if any(token in values for token in tokens):
                return group
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

    @staticmethod
    def _plain_text(html: str) -> str | None:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _images(product: dict[str, Any]) -> list[str]:
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
