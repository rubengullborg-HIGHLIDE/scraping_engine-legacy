"""Full-catalog importer for Skagen Clothing men's clothing and store stock."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://skagen-clothing.dk"
MEN_COLLECTION_HANDLE = "alt-toj-til-maend"
EXCLUDED_PRODUCT_TYPES = frozenset({"accessories", "beanie", "gavekort", "mystery box"})
TRACKED_STORES = {
    "skagen-aarhus": {
        "name": "Skagen Clothing Aarhus",
        "source_name": "Aarhus Butik - Skagen Clothing",
        "address": "Store Torv 14, 8000 Aarhus C",
    },
    "skagen-copenhagen": {
        "name": "Skagen Clothing Copenhagen",
        "source_name": "Skagen Clothing Copenhagen",
        "address": "Klosterstræde 10, 1157 København K",
    },
}


class SkagenClothingScraper:
    """Read Skagen Clothing's Shopify catalogue and rendered exact inventory."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/html,*/*",
        })

    def discover_products(self, collection_handle: str = MEN_COLLECTION_HANDLE) -> list[dict[str, Any]]:
        """Discover clothing from the broad men's collection and exclude non-clothing types."""
        products: dict[int, dict[str, Any]] = {}
        page = 1
        while True:
            response = self.session.get(
                f"{BASE_URL}/collections/{collection_handle}/products.json",
                params={"limit": 250, "page": page},
                timeout=30,
            )
            response.raise_for_status()
            batch = response.json().get("products", [])
            if not batch:
                break
            for product in batch:
                if product.get("id") is not None and self.is_clothing(product):
                    product_id = int(product["id"])
                    product["_discovery_collections"] = [collection_handle]
                    products[product_id] = product
            if len(batch) < 250:
                break
            page += 1
        return list(products.values())

    @staticmethod
    def is_clothing(product: dict[str, Any]) -> bool:
        product_type = str(product.get("type") or product.get("product_type") or "").strip().casefold()
        return product_type not in EXCLUDED_PRODUCT_TYPES

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}.js", timeout=30)
        response.raise_for_status()
        return response.json()

    def fetch_product_page(self, url_or_handle: str) -> str:
        """Fetch fresh rendered HTML because this response contains the local quantities."""
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(
            f"{BASE_URL}/products/{handle}",
            params={"highlide_stock_check": int(time.time_ns())},
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
            timeout=30,
        )
        response.raise_for_status()
        return response.text

    def product_to_row(self, product: dict[str, Any], page_html: str) -> dict[str, Any]:
        """Build one row per Shopify product; each product represents one colour."""
        product_id = int(product["id"])
        handle = product.get("handle") or self._handle_from_url(str(product_id))
        canonical_url = f"{BASE_URL}/products/{handle}"
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        details = self._page_details(page_html, product)
        inventory, local_by_variant, inventory_source = self._inventory_from_page(page_html, variants)
        color = details["color"] or self._color_from_title(product.get("title"))
        sku_style_reference = self._style_reference(variants, color, product_id)
        source_parent_id = self._parent_reference(handle, details["color_variants"], sku_style_reference)

        webshop_sizes: list[dict[str, Any]] = []
        for variant in variants:
            variant_id = str(variant["id"])
            size = self._text(variant.get("public_title") or variant.get("title"))
            local = local_by_variant.get(variant_id, {})
            local_stores = [
                slug for slug in TRACKED_STORES
                if bool((local.get(slug) or {}).get("available"))
            ]
            known_stock = sum(
                state["stock"]
                for state in local.values()
                if isinstance(state, dict) and isinstance(state.get("stock"), int)
            )
            local_stock_known = bool(local) and all(slug in local for slug in TRACKED_STORES)
            webshop_sizes.append({
                "size": size,
                "in_stock": bool(variant.get("available")),
                "stock": None,
                "stock_known": False,
                "local_available": bool(local_stores),
                "local_stores": local_stores,
                "local_stock": known_stock if local_stock_known else None,
                "local_stock_known": local_stock_known,
                "source_size_variant_id": variant_id,
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })

        current_price = self._price(product.get("price"))
        list_price = self._price(product.get("compare_at_price"))
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None

        stores = inventory["stores"]
        local_total_stock = sum(
            store["total_stock"]
            for store in stores.values()
            if isinstance(store.get("total_stock"), int)
        )
        aarhus = stores["skagen-aarhus"]
        scraped_at = datetime.now(timezone.utc).isoformat()
        specifications = {
            "product_type": product.get("type") or None,
            "fit_indicator": details["fit_indicator"],
        }
        specifications = {key: value for key, value in specifications.items() if value}

        return {
            "source_parent_id": source_parent_id,
            "source_color_id": str(product_id),
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "source_product_number": sku_style_reference,
            "name": product.get("title") or None,
            "brand": product.get("vendor") or None,
            "product_type": product.get("type") or None,
            "color": color,
            "color_group": self._color_group(color),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": details["description"],
            "highlights": details["highlights"],
            "specifications": specifications,
            "materials": details["materials"],
            "fit": details["fit"],
            "category": self._category(product),
            "category_path": self._category_path(product),
            "tags": self._strings(product.get("tags")),
            "images": self._images(product),
            "model_info": details["model_info"],
            "size_guide": details["size_guide"],
            "webshop_sizes": webshop_sizes,
            "local_inventory": inventory,
            "local_total_stock": local_total_stock,
            "local_available": any(store.get("available") for store in stores.values()),
            "aarhus_total_stock": aarhus.get("total_stock") if aarhus.get("stock_known") else None,
            "aarhus_available": bool(aarhus.get("available")),
            "inventory_checked_at": scraped_at,
            "raw": {
                "shopify_product_id": str(product_id),
                "shopify_handle": handle,
                "shopify_product_type": product.get("type"),
                "sku_style_reference": sku_style_reference,
                "variant_skus": {
                    str(variant["id"]): variant.get("sku")
                    for variant in variants
                },
                "discovery_collections": product.get("_discovery_collections") or [],
                "color_variants": details["color_variants"],
                "inventory_locations": inventory_source,
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _page_details(cls, html: str, product: dict[str, Any]) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        description_html = product.get("description") or product.get("content") or ""
        description_soup = BeautifulSoup(description_html, "html.parser")
        paragraphs = [node.get_text(" ", strip=True) for node in description_soup.find_all("p")]
        highlights = cls._dedupe(
            node.get_text(" ", strip=True)
            for node in description_soup.select("li")
            if node.get_text(" ", strip=True)
        )
        description = "\n\n".join(text for text in paragraphs if text) or cls._plain_text(description_html)
        all_lines = cls._dedupe([*paragraphs, *highlights])
        color, color_variants = cls._color_variants(soup)
        return {
            "description": description,
            "highlights": highlights,
            "materials": cls._materials(description_soup, all_lines),
            "fit": cls._fit(all_lines),
            "fit_indicator": cls._fit_indicator(soup),
            "model_info": cls._model_info(soup),
            "size_guide": cls._size_guide(soup),
            "color": color,
            "color_variants": color_variants,
        }

    @classmethod
    def _inventory_from_page(
        cls,
        html: str,
        variants: Iterable[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
        soup = BeautifulSoup(html, "html.parser")
        stores = {
            slug: cls._empty_store(config["name"], config["address"])
            for slug, config in TRACKED_STORES.items()
        }
        size_by_variant = {
            str(variant["id"]): cls._text(variant.get("public_title") or variant.get("title"))
            for variant in variants
            if variant.get("id") is not None
        }
        by_variant: dict[str, dict[str, Any]] = {}
        raw: list[dict[str, Any]] = []
        node = soup.select_one("[data-variant-inventories]")
        payload = node.get("data-variant-inventories") if node else None
        try:
            inventories = json.loads(str(payload)) if payload else []
        except (json.JSONDecodeError, TypeError):
            inventories = []
        if not inventories:
            return {"stores": stores}, by_variant, raw

        for store in stores.values():
            store["stock_known"] = True
            store["total_stock"] = 0

        for item in inventories:
            inventory = item.get("inventory") or {}
            variant_id = str(inventory.get("variant_id") or "").rsplit("/", 1)[-1]
            size = size_by_variant.get(variant_id)
            if not variant_id:
                continue
            by_variant[variant_id] = {}
            raw_levels: list[dict[str, Any]] = []
            for level in inventory.get("inventoryLevels") or []:
                location = level.get("location") or {}
                stock = cls._integer(level.get("available"))
                raw_level = {"available": stock, "location": location}
                raw_levels.append(raw_level)
                slug = cls._store_slug(location)
                if not slug:
                    continue
                available = bool(stock and stock > 0)
                by_variant[variant_id][slug] = {"available": available, "stock": stock}
                if size:
                    stores[slug]["sizes"][size] = {"available": available, "stock": stock}
                stores[slug]["available"] = stores[slug]["available"] or available
                stores[slug]["total_stock"] += stock or 0
            raw.append({"variant_id": variant_id, "inventory_levels": raw_levels})

        for variant_id, size in size_by_variant.items():
            by_variant.setdefault(variant_id, {})
            for slug, store in stores.items():
                state = by_variant[variant_id].setdefault(slug, {"available": False, "stock": 0})
                if size and size not in store["sizes"]:
                    store["sizes"][size] = state.copy()
        return {"stores": stores}, by_variant, raw

    @staticmethod
    def _store_slug(location: dict[str, Any]) -> str | None:
        name = str(location.get("name") or "").casefold()
        city = str(location.get("city") or "").casefold()
        zip_code = str(location.get("zip") or "")
        if name == TRACKED_STORES["skagen-aarhus"]["source_name"].casefold() or (
            "aarhus" in name and zip_code == "8000"
        ):
            return "skagen-aarhus"
        if name == TRACKED_STORES["skagen-copenhagen"]["source_name"].casefold() or (
            ("københavn" in city or "copenhagen" in name) and zip_code == "1157"
        ):
            return "skagen-copenhagen"
        return None

    @classmethod
    def _color_variants(cls, soup: BeautifulSoup) -> tuple[str | None, list[dict[str, Any]]]:
        heading = next(
            (node for node in soup.find_all("p") if node.get_text(" ", strip=True).casefold().startswith("farve")),
            None,
        )
        if not heading:
            return None, []
        color_node = heading.find("span")
        color = color_node.get_text(" ", strip=True) if color_node else None
        color_variants: list[dict[str, Any]] = []
        swatches = heading.find_next("ul")
        if swatches:
            for link in swatches.select("a[aria-label]"):
                href = str(link.get("href") or "")
                label = cls._text(link.get("aria-label"))
                handle = cls._handle_from_url(href) if "/products/" in href else None
                item = {
                    "color": label,
                    "handle": handle,
                    "url": f"{BASE_URL}/products/{handle}" if handle else None,
                    "current": link.get("aria-current") == "true" or href == "#",
                }
                color_variants.append(item)
        return color, color_variants

    @classmethod
    def color_handles(cls, page_html: str) -> list[str]:
        _, variants = cls._color_variants(BeautifulSoup(page_html, "html.parser"))
        return cls._dedupe(item["handle"] for item in variants if item.get("handle"))

    @classmethod
    def _materials(cls, description_soup: BeautifulSoup, lines: list[str]) -> list[str]:
        del lines  # The HTML text is scanned as a whole so paragraph formatting cannot break a composition.
        candidates: list[str] = []
        text = description_soup.get_text(" ", strip=True)
        for match in re.finditer(
            r"\d{1,3}\s*%\s*(?:bomuld|cotton|elastan|hør|linen|nylon|polyamid|polyester|viskose|uld|wool|akryl)"
            r"(?:\s*(?:,|/|&|\+|og)\s*\d{1,3}\s*%\s*"
            r"(?:bomuld|cotton|elastan|hør|linen|nylon|polyamid|polyester|viskose|uld|wool|akryl))*",
            text,
            flags=re.IGNORECASE,
        ):
            candidates.append(re.sub(r"\s+", " ", match.group(0)).strip(" ,/+"))
        return cls._dedupe(candidates)

    @classmethod
    def _fit(cls, lines: list[str]) -> str | None:
        patterns = (
            r"\b(?:extra\s+baggy|baggy|boxy|light\s+box|loose|oversized|regular|relaxed|slim)\s+(?:fit|pasform)\b",
            r"\bpasformen\s+er\s+[^.!]+",
            r"\b(?:løs|løst|tæt|tætsiddende|klassisk)\w*\s+(?:fit|pasform|siddende)\b",
        )
        for line in lines:
            for pattern in patterns:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if match:
                    return match.group(0).strip()
        return None

    @staticmethod
    def _fit_indicator(soup: BeautifulSoup) -> str | None:
        heading = next(
            (node for node in soup.find_all(["p", "h4"]) if node.get_text(" ", strip=True).casefold() == "pasform"),
            None,
        )
        section = heading.find_parent("section") if heading else None
        if not section:
            return None
        labels = section.select("ul.grid-cols-3:last-of-type li")
        bars = section.select("ul.grid-cols-3:first-of-type li")
        for label, bar in zip(labels, bars):
            if "bg-black" in (bar.get("class") or []):
                return label.get_text(" ", strip=True) or None
        return None

    @staticmethod
    def _model_info(soup: BeautifulSoup) -> dict[str, Any]:
        node = next(
            (item for item in soup.find_all("p") if "modellen" in item.get_text(" ", strip=True).casefold()),
            None,
        )
        if not node:
            return {}
        text = node.get_text(" ", strip=True)
        result: dict[str, Any] = {"text": text}
        matches = list(re.finditer(r"(\d{3})\s*cm", text, flags=re.IGNORECASE))
        if matches:
            result["height_cm"] = int(matches[0].group(1))
        size = re.search(r"(?:bruger|bærer)\s+(?:str(?:\.|ørrelse)?\s*)?([^,;\-]+)", text, flags=re.IGNORECASE)
        if size:
            result["wears_size"] = size.group(1).strip()
        return result

    @staticmethod
    def _size_guide(soup: BeautifulSoup) -> dict[str, Any]:
        for content in soup.select('[aria-label="Size Guide"], [aria-label*="Size guide" i]'):
            image = content.find("img")
            if image and (source := image.get("src") or image.get("data-src")):
                return {"type": "image", "image_url": urljoin(BASE_URL, str(source))}
        return {}

    @classmethod
    def _style_reference(cls, variants: list[dict[str, Any]], color: str | None, product_id: int) -> str:
        skus = [str(variant.get("sku") or "").strip() for variant in variants if variant.get("sku")]
        sku = skus[0] if skus else ""
        if not sku:
            return str(product_id)
        base = sku
        if len(skus) > 1:
            common_prefix = skus[0]
            for candidate in skus[1:]:
                common_prefix = common_prefix[: next(
                    (index for index, pair in enumerate(zip(common_prefix, candidate)) if pair[0].casefold() != pair[1].casefold()),
                    min(len(common_prefix), len(candidate)),
                )]
            if "-" in common_prefix:
                base = common_prefix.rsplit("-", 1)[0]
        size_tokens: list[str] = []
        for variant in variants:
            size = str(variant.get("public_title") or variant.get("title") or "")
            size_tokens.extend(token for token in re.split(r"[^0-9A-Za-zÆØÅæøå]+", size) if token)
            normalized = re.sub(r"[^0-9A-Za-zÆØÅæøå]+", "-", size).strip("-")
            if normalized:
                size_tokens.append(normalized)
        for token in sorted(cls._dedupe(size_tokens), key=len, reverse=True):
            if token and base.casefold().endswith(f"-{token}".casefold()):
                base = base[: -(len(token) + 1)]
                break
        color_tokens = [token for token in re.split(r"[^0-9A-Za-zÆØÅæøå]+", color or "") if token]
        for token in sorted(color_tokens, key=len, reverse=True):
            base = re.sub(rf"(?:^|-)({re.escape(token)})(?=-|$)", "", base, flags=re.IGNORECASE).strip("-")
        return re.sub(r"-{2,}", "-", base) or str(product_id)

    @classmethod
    def _parent_reference(
        cls,
        current_handle: str,
        color_variants: list[dict[str, Any]],
        sku_style_reference: str,
    ) -> str:
        """Use Skagen's swatch family when its colour SKUs use inconsistent prefixes."""
        handles = cls._dedupe([
            current_handle,
            *(item.get("handle") for item in color_variants if item.get("handle")),
        ])
        if len(handles) < 2:
            return sku_style_reference
        common_prefix = handles[0]
        for handle in handles[1:]:
            common_prefix = common_prefix[: next(
                (index for index, pair in enumerate(zip(common_prefix, handle)) if pair[0] != pair[1]),
                min(len(common_prefix), len(handle)),
            )]
        common_prefix = common_prefix.rstrip("-")
        if "-" in common_prefix and len(common_prefix) >= 8:
            return common_prefix
        return sku_style_reference

    @staticmethod
    def _category(product: dict[str, Any]) -> str | None:
        product_type = SkagenClothingScraper._text(product.get("type") or product.get("product_type"))
        if product_type:
            return product_type
        tags = SkagenClothingScraper._strings(product.get("tags"))
        preferred = next((tag for tag in tags if tag.upper().endswith(" TIL MÆND") and tag.upper() != "ALT TØJ TIL MÆND"), None)
        return preferred.title() if preferred else None

    @classmethod
    def _category_path(cls, product: dict[str, Any]) -> list[str]:
        category = cls._category(product)
        return ["Herretøj", category] if category else ["Herretøj"]

    @staticmethod
    def _color_from_title(title: Any) -> str | None:
        text = str(title or "").strip()
        colors = (
            "washed grey", "light grey", "dark grey", "light blue", "dark blue", "dark brown",
            "heather grey", "mid blue", "off white", "black", "sort", "white", "hvid", "grey",
            "grå", "blue", "blå", "navy", "indigo", "brown", "brun", "beige", "army", "green",
            "grøn", "pink", "mokka", "sand", "darkwash", "limewash",
        )
        lowered = text.casefold()
        return next((color for color in colors if lowered.endswith(color.casefold())), None)

    @staticmethod
    def _color_group(color: str | None) -> str | None:
        if not color:
            return None
        normalized = color.casefold()
        groups = (
            ("Black", ("black", "sort")),
            ("White", ("white", "hvid", "cream", "ivory")),
            ("Grey", ("grey", "gray", "grå", "silver")),
            ("Blue", ("blue", "blå", "navy", "indigo", "darkwash")),
            ("Green", ("green", "grøn", "army", "olive")),
            ("Brown", ("brown", "brun", "mokka", "beige", "sand", "tan")),
            ("Red", ("red", "rød", "burgundy")),
            ("Pink", ("pink", "rosa")),
            ("Yellow", ("yellow", "gul")),
            ("Orange", ("orange",)),
            ("Purple", ("purple", "lilla")),
        )
        return next((group for group, words in groups if any(word in normalized for word in words)), None)

    @staticmethod
    def _price(value: Any) -> float | None:
        return None if value is None or value == "" else float(value) / 100

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _text(value: Any) -> str | None:
        text = str(value).strip() if value is not None else ""
        return text or None

    @classmethod
    def _strings(cls, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return cls._dedupe(cls._text(value) for value in values if cls._text(value))

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
        value = str(url_or_handle).split("?", 1)[0].rstrip("/")
        return value.rsplit("/products/", 1)[-1].rsplit("/", 1)[-1]

    @staticmethod
    def _empty_store(name: str, address: str) -> dict[str, Any]:
        return {
            "name": name,
            "address": address,
            "stock_known": False,
            "available": False,
            "total_stock": None,
            "sizes": {},
        }

    @staticmethod
    def _dedupe(values: Iterable[Any]) -> list[Any]:
        result: list[Any] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result
