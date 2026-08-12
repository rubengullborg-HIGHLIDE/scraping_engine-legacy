"""Full-catalog importer for Rains men's clothing and exact Danish store stock."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.dk.rains.com"
INVENTORY_URL = "https://rains-locations-api.vercel.app/api/get-inventory"

# Rains' two broad men's collections omit a substantial part of the catalogue.
# These navigation subcollections are intentionally combined and deduplicated.
MEN_COLLECTION_HANDLES = (
    "mens-clothing",
    "mens-outerwear",
    "mens-sweatshirts-hoodies",
    "knitwear/mand",
    "mens-t-shirts",
    "mens-polos",
    "mens-track-jackets",
    "mens-bottoms",
    "mens-fleece",
    "woven",
    "jackets-men",
    "mens-light-jackets",
    "mens-shell-jackets",
    "mens-bomber-jackets",
    "mens-quilted-jackets",
    "mens-vests",
    "pants-men",
    "mens-winter-jackets",
    "puffer-jackets-men",
)

TRACKED_WAREHOUSES = {
    2: {"slug": "rains-copenhagen", "name": "Rains København, Amagertorv"},
    3: {"slug": "rains-aarhus", "name": "Rains Aarhus, Klostertorv"},
    4: {"slug": "rains-frederiksberg", "name": "Rains Frederiksberg, Gammel Kongevej"},
}

DETAIL_KEYS = {
    "materiale": "materials",
    "materials": "materials",
    "pasform": "fit",
    "fit": "fit",
    "vandtaet": "waterproof",
    "waterproof": "waterproof",
    "vandsojletryk": "water_column_pressure",
    "vandsøjletryk": "water_column_pressure",
    "vindtæt": "windproof",
    "vindtaet": "windproof",
    "windproof": "windproof",
    "lukning": "closure",
    "closure": "closure",
    "vaegt": "weight",
    "vægt": "weight",
    "weight": "weight",
    "funktioner": "features",
    "features": "features",
}

COLOR_GROUP_KEYWORDS = (
    ("Sort", ("black", "jet", "ink", "shadow")),
    ("Hvid", ("white", "off white", "cream", "bone", "chalk", "blanc")),
    ("Grå", ("grey", "gray", "slate", "cinder", "cement", "fog", "silver", "charcoal", "asphalt")),
    ("Blå", ("blue", "navy", "sea", "ocean", "sky", "cobalt", "aqua", "teal", "harbor", "harbour", "mystique", "lucid")),
    ("Grøn", ("green", "olive", "khaki", "moss", "sage", "forest", "fir", "envy", "well")),
    ("Brun", ("brown", "bark", "soil", "taupe", "coffee", "chocolate", "umber", "splinter", "comet")),
    ("Beige", ("beige", "sand", "dune", "tan", "camel", "oat")),
    ("Rød", ("red", "burgundy", "wine", "ember", "maroon", "tempt")),
    ("Orange", ("orange", "rust", "copper")),
    ("Gul", ("yellow", "mustard", "lemon", "glow")),
    ("Lilla", ("purple", "violet", "lavender", "lilac", "plum")),
    ("Pink", ("pink", "rose", "blush")),
    ("Multifarvet", ("multi", "stripe", "check", "print", "pattern")),
)


class RainsScraper:
    """Read Rains' Shopify catalogue and its public exact store inventory."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/plain,*/*",
        })

    def discover_products(
        self,
        collection_handles: Iterable[str] = MEN_COLLECTION_HANDLES,
    ) -> list[dict[str, Any]]:
        """Return unique styles from every relevant men's clothing collection."""
        by_id: dict[int, dict[str, Any]] = {}
        collections_by_id: dict[int, set[str]] = {}
        for collection_handle in collection_handles:
            feed_handle, _, collection_tag = collection_handle.partition("/")
            page = 1
            while True:
                response = self.session.get(
                    f"{BASE_URL}/collections/{feed_handle}/products.json",
                    params={"limit": 250, "page": page},
                    timeout=30,
                )
                response.raise_for_status()
                batch = response.json().get("products", [])
                if not batch:
                    break
                for product in batch:
                    if product.get("id") is None:
                        continue
                    if collection_tag and collection_tag.casefold() not in {
                        str(tag).casefold() for tag in product.get("tags", [])
                    }:
                        continue
                    product_id = int(product["id"])
                    by_id[product_id] = product
                    collections_by_id.setdefault(product_id, set()).add(collection_handle)
                if len(batch) < 250:
                    break
                page += 1

        products = list(by_id.values())
        # "woven" is shared by Rains' menswear navigation and occasionally
        # returns the separately-published women's counterpart of a unisex
        # style.  Those pages have distinct Shopify ids but the same Rains
        # style/colour SKU as their "-male" counterparts, which would make a
        # later import overwrite the men's source URL with a women's one.
        products = [
            product
            for product in products
            if not self._is_womens_product_handle(product.get("handle"))
        ]
        for product in products:
            product["_collection_handles"] = sorted(collections_by_id[int(product["id"])])
        return products

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}.js", timeout=30)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _is_womens_product_handle(handle: Any) -> bool:
        """Identify Rains' gender-specific women's product pages.

        Rains uses both ``-female`` and a standalone ``-w-`` / ``-w`` token
        in Shopify handles for women's product pages.  Do not remove unisex
        products: neither convention is used in their handles.
        """
        if not isinstance(handle, str):
            return False
        normalized = handle.casefold().rstrip("/")
        return normalized.endswith("-female") or bool(re.search(r"(?:^|-)w(?:-|$)", normalized))

    def fetch_product_page(self, url_or_handle: str) -> str:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}", timeout=30)
        response.raise_for_status()
        return response.text

    def fetch_inventory(self) -> dict[str, Any]:
        """Fetch and index all three Danish Rains warehouses in one request."""
        response = self.session.get(INVENTORY_URL, params={"locale": "dk"}, timeout=60)
        response.raise_for_status()
        payload = response.json()
        warehouses = payload.get("warehouses")
        if not isinstance(warehouses, list):
            raise RuntimeError("Rains inventory endpoint returned an unexpected response.")

        metadata: dict[int, dict[str, Any]] = {}
        stock_by_warehouse: dict[int, dict[str, int]] = {}
        for warehouse in warehouses:
            if warehouse.get("warehouseid") is None:
                continue
            warehouse_id = int(warehouse["warehouseid"])
            if warehouse_id not in TRACKED_WAREHOUSES:
                continue
            metadata[warehouse_id] = {
                key: warehouse.get(key)
                for key in (
                    "warehouseid", "externalid", "storeid", "eshopid", "name",
                    "address", "address2", "countryid", "email", "phone", "sellable", "usetype",
                )
            }
            stock: dict[str, int] = {}
            for item in warehouse.get("parsedItems") or []:
                sku = self._normalize_inventory_sku(item.get("sku"))
                if not sku:
                    continue
                quantity = self._quantity(item.get("decimaltotal"))
                stock[sku] = max(stock.get(sku, 0), quantity)
            stock_by_warehouse[warehouse_id] = stock

        missing = sorted(set(TRACKED_WAREHOUSES) - set(metadata))
        if missing:
            raise RuntimeError(f"Rains inventory endpoint omitted expected warehouses: {missing}")

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "locale": payload.get("locale"),
            "warehouses": metadata,
            "stock_by_warehouse": stock_by_warehouse,
        }

    def build_catalog_rows(
        self,
        products: Iterable[tuple[dict[str, Any], str]],
        inventory_snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for product, page_html in products:
            rows.extend(self.product_to_rows(product, page_html, inventory_snapshot))
        return rows

    def product_to_rows(
        self,
        product: dict[str, Any],
        page_html: str,
        inventory_snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        product_id = int(product["id"])
        handle = product.get("handle") or self._handle_from_url(str(product_id))
        canonical_url = f"{BASE_URL}/products/{handle}"
        details = self._page_details(page_html)
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        variants_by_color = self._variants_by_color(product, variants)
        scraped_at = datetime.now(timezone.utc).isoformat()
        rows: list[dict[str, Any]] = []

        for color, color_variants in variants_by_color.items():
            sku_prefixes = [self._sku_prefix(variant.get("sku")) for variant in color_variants]
            sku_prefix = next((prefix for prefix, _ in Counter(filter(None, sku_prefixes)).most_common(1)), None)
            style_reference = self._style_reference(sku_prefix) or str(product_id)
            source_color_id = sku_prefix or f"color-{self._slug(color)}"
            inventory = self._local_inventory(color_variants, inventory_snapshot, product)
            webshop_sizes = [self._webshop_size(variant, product) for variant in color_variants]
            current_price, list_price = self._color_prices(color_variants)
            aarhus = inventory["stores"]["rains-aarhus"]
            first_variant_id = int(color_variants[0]["id"])

            rows.append({
                "source_parent_id": style_reference,
                "source_color_id": source_color_id,
                "source_url": f"{canonical_url}?variant={first_variant_id}",
                "canonical_url": canonical_url,
                "source_product_number": style_reference,
                "name": product.get("title") or None,
                "brand": product.get("vendor") or "Rains",
                "product_type": product.get("type") or None,
                "color": color,
                "color_group": self._color_group(color),
                "current_price": current_price,
                "list_price": list_price,
                "currency": "DKK",
                "description": self._plain_text(product.get("description") or product.get("content") or ""),
                "highlights": details["highlights"],
                "specifications": details["specifications"],
                "materials": details["materials"],
                "fit": details["fit"],
                "care_instructions": details["care_instructions"],
                "category": details["category_path"][-1] if details["category_path"] else product.get("type") or None,
                "category_path": details["category_path"],
                "collections": sorted(set(product.get("_collection_handles") or [])),
                "tags": [str(tag) for tag in product.get("tags", [])],
                "images": self._color_images(product, color, color_variants),
                "model_info": details["model_info"],
                "size_guide": details["size_guide"],
                "webshop_sizes": webshop_sizes,
                "local_inventory": inventory,
                "local_total_stock": sum(store["total_stock"] for store in inventory["stores"].values()),
                "local_available": any(store["available"] for store in inventory["stores"].values()),
                "aarhus_total_stock": aarhus["total_stock"],
                "aarhus_available": aarhus["available"],
                "inventory_checked_at": inventory_snapshot["checked_at"],
                "raw": {
                    "shopify_product_id": str(product_id),
                    "shopify_handle": handle,
                    "shopify_color": color,
                    "normalized_color_sku_prefix": sku_prefix,
                    "product_type": product.get("type"),
                    "published_at": product.get("published_at"),
                    "created_at": product.get("created_at"),
                    "custom_metafields": details["custom_metafields"],
                    "source_warehouses": {
                        str(key): value for key, value in inventory_snapshot["warehouses"].items()
                    },
                    "source_variants": [self._raw_variant(variant) for variant in color_variants],
                },
                "scraped_at": scraped_at,
                "updated_at": scraped_at,
            })
        return rows

    def _local_inventory(
        self,
        variants: list[dict[str, Any]],
        inventory_snapshot: dict[str, Any],
        product: dict[str, Any],
    ) -> dict[str, Any]:
        inventory = {
            "stores": {
                source["slug"]: self._empty_store(source["name"])
                for source in TRACKED_WAREHOUSES.values()
            }
        }
        for variant in variants:
            size = self._variant_size(variant, product)
            sku = self._normalize_inventory_sku(variant.get("sku"))
            barcode = self._normalize_inventory_sku(variant.get("barcode"))
            for warehouse_id, source in TRACKED_WAREHOUSES.items():
                stock_index = inventory_snapshot["stock_by_warehouse"][warehouse_id]
                if sku and sku in stock_index:
                    quantity = stock_index[sku]
                elif barcode and barcode in stock_index:
                    quantity = stock_index[barcode]
                else:
                    quantity = 0
                store = inventory["stores"][source["slug"]]
                store["total_stock"] += quantity
                store["available"] = store["available"] or quantity > 0
                store["sizes"][size] = {"available": quantity > 0, "stock": quantity}
        return inventory

    @classmethod
    def _page_details(cls, html: str) -> dict[str, Any]:
        soup = BeautifulSoup(html, "html.parser")
        custom_metafields = cls._custom_metafields(soup)
        specifications, materials, highlights, fit = cls._product_details(soup)
        category_path = [
            part.strip()
            for part in str(custom_metafields.get("full_category_path") or "").split("/")
            if part.strip()
        ]
        sizeguide = custom_metafields.get("sizeguide")
        size_guide = {"unit": "cm", "measurements": sizeguide} if isinstance(sizeguide, list) else {}
        care = cls._care_instructions(soup, custom_metafields.get("care_instructions"))
        return {
            "specifications": specifications,
            "materials": materials,
            "highlights": highlights,
            "fit": fit,
            "care_instructions": care,
            "category_path": category_path,
            "size_guide": size_guide,
            "model_info": cls._model_info(soup),
            "custom_metafields": custom_metafields,
        }

    @classmethod
    def _product_details(
        cls,
        soup: BeautifulSoup,
    ) -> tuple[dict[str, Any], list[str], list[str], str | None]:
        specifications: dict[str, Any] = {}
        materials: list[str] = []
        highlights: list[str] = []
        fit: str | None = None
        content = soup.select_one("#product-tab-details .space-y-4")
        if not content:
            return specifications, materials, highlights, fit

        for block in content.find_all("div", recursive=False):
            heading = block.find("p", class_=lambda value: value and "heading" in value.split())
            if not heading:
                continue
            label = heading.get_text(" ", strip=True).rstrip(":")
            heading.extract()
            lines = [line.strip(" -\t") for line in block.get_text("\n", strip=True).splitlines() if line.strip(" -\t")]
            key = DETAIL_KEYS.get(cls._slug(label), cls._slug(label).replace("-", "_"))
            if key == "features":
                highlights = lines
                specifications[key] = lines
            elif key == "materials":
                materials = lines
                specifications[key] = lines
            else:
                value = "\n".join(lines)
                if value:
                    specifications[key] = value
                    if key == "fit":
                        fit = value

        if not fit:
            fit = next(
                (highlight for highlight in highlights if re.search(r"\b(fit|pasform)\b", highlight, re.IGNORECASE)),
                None,
            )
        return specifications, materials, highlights, fit

    @staticmethod
    def _custom_metafields(soup: BeautifulSoup) -> dict[str, Any]:
        result: dict[str, Any] = {}
        assignment = re.compile(
            r'window\.productShopStape\.metafields\["custom"\]\["(?P<key>[^"]+)"\]\s*=\s*'
            r'(?P<value>.*?);(?=\s*window\.productShopStape|\s*$)',
            flags=re.DOTALL,
        )
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if "window.productShopStape.metafields" not in text:
                continue
            for match in assignment.finditer(text.strip()):
                try:
                    result[match.group("key")] = json.loads(match.group("value"))
                except json.JSONDecodeError:
                    continue
        return result

    @staticmethod
    def _care_instructions(soup: BeautifulSoup, source_care: Any) -> list[dict[str, Any]]:
        localized = [
            item.get_text(" ", strip=True)
            for item in soup.select("#product-tab-care-instructions li")
            if item.get_text(" ", strip=True)
        ]
        source_items = source_care if isinstance(source_care, list) else []
        result: list[dict[str, Any]] = []
        for index in range(max(len(localized), len(source_items))):
            source = source_items[index] if index < len(source_items) and isinstance(source_items[index], dict) else {}
            item: dict[str, Any] = {
                "code": source.get("Code") or source.get("code"),
                "description": source.get("Description") or source.get("description"),
                "localized_description": localized[index] if index < len(localized) else None,
            }
            result.append({key: value for key, value in item.items() if value is not None})
        return result

    @staticmethod
    def _model_info(soup: BeautifulSoup) -> dict[str, Any]:
        pattern = re.compile(
            r"Modellen er\s+(?P<height>\d+)\s*cm.*?bruger størrelse\s+(?P<size>[A-Z0-9/.-]+)",
            flags=re.IGNORECASE,
        )
        models: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()
        for text in soup.stripped_strings:
            match = pattern.search(text)
            if not match:
                continue
            key = (int(match.group("height")), match.group("size"))
            if key in seen:
                continue
            seen.add(key)
            models.append({"height_cm": key[0], "size": key[1], "text": text})
        return {"models": models} if models else {}

    @classmethod
    def _variants_by_color(
        cls,
        product: dict[str, Any],
        variants: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        color_position = cls._option_position(product, {"color", "colour", "farve"}) or 1
        result: dict[str, list[dict[str, Any]]] = {}
        for variant in variants:
            color = str(variant.get(f"option{color_position}") or "Default").strip()
            result.setdefault(color, []).append(variant)
        return result

    @staticmethod
    def _option_position(product: dict[str, Any], names: set[str]) -> int | None:
        for index, option in enumerate(product.get("options", []), start=1):
            name = option.get("name") if isinstance(option, dict) else option
            if str(name).strip().lower() in names:
                return int(option.get("position") or index) if isinstance(option, dict) else index
        return None

    @classmethod
    def _variant_size(cls, variant: dict[str, Any], product: dict[str, Any] | None = None) -> str:
        size_position = cls._option_position(product, {"size", "størrelse"}) if product else 2
        if product is not None and size_position is None:
            return "One Size"
        value = variant.get(f"option{size_position}") if size_position else None
        return str(value or variant.get("public_title") or variant.get("title") or "One Size").strip()

    @classmethod
    def _webshop_size(cls, variant: dict[str, Any], product: dict[str, Any]) -> dict[str, Any]:
        return {
            "size": cls._variant_size(variant, product),
            "in_stock": bool(variant.get("available")),
            "stock": None,
            "stock_known": False,
            "source_size_variant_id": str(variant["id"]),
            "source_product_number": cls._normalize_inventory_sku(variant.get("sku")) or None,
            "barcode": variant.get("barcode") or None,
        }

    @classmethod
    def _color_prices(cls, variants: list[dict[str, Any]]) -> tuple[float | None, float | None]:
        prices = [price for variant in variants if (price := cls._price(variant.get("price"))) is not None]
        current_price = min(prices) if prices else None
        compare_prices = [
            price for variant in variants if (price := cls._price(variant.get("compare_at_price"))) is not None
        ]
        list_price = max(compare_prices) if compare_prices else None
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None
        return current_price, list_price

    @classmethod
    def _color_images(
        cls,
        product: dict[str, Any],
        color: str,
        variants: list[dict[str, Any]],
    ) -> list[str]:
        media = [item for item in product.get("media", []) if item.get("media_type") == "image"]
        color_starts: dict[str, int] = {}
        for variant in product.get("variants", []):
            featured = variant.get("featured_image") or {}
            if featured.get("position") is None:
                continue
            variant_color = str(variant.get("option1") or "Default").strip()
            color_starts.setdefault(variant_color, int(featured["position"]))
        start = color_starts.get(color)
        if start is not None:
            later_starts = sorted(position for position in color_starts.values() if position > start)
            end = later_starts[0] if later_starts else None
            urls = [
                cls._absolute_url(item.get("src"))
                for item in media
                if item.get("src") and int(item.get("position") or 0) >= start
                and (end is None or int(item.get("position") or 0) < end)
            ]
            if urls:
                return cls._deduplicate(urls)

        featured_urls = [
            cls._absolute_url((variant.get("featured_image") or {}).get("src"))
            for variant in variants
            if (variant.get("featured_image") or {}).get("src")
        ]
        if featured_urls:
            return cls._deduplicate(featured_urls)
        return cls._images(product)

    @staticmethod
    def _raw_variant(variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(variant.get("id")),
            "title": variant.get("title"),
            "sku": variant.get("sku"),
            "normalized_sku": RainsScraper._normalize_inventory_sku(variant.get("sku")),
            "barcode": variant.get("barcode"),
            "available": bool(variant.get("available")),
            "price": variant.get("price"),
            "compare_at_price": variant.get("compare_at_price"),
            "weight_grams": variant.get("weight") or variant.get("grams"),
        }

    @staticmethod
    def _price(value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, str) and "." in value:
            return float(value)
        return float(value) / 100

    @staticmethod
    def _quantity(value: Any) -> int:
        try:
            return max(0, int(Decimal(str(value or 0))))
        except (InvalidOperation, ValueError):
            return 0

    @staticmethod
    def _normalize_inventory_sku(value: Any) -> str:
        if value is None:
            return ""
        return "-".join(part.strip() for part in str(value).strip().split("\\") if part.strip())

    @classmethod
    def _sku_prefix(cls, value: Any) -> str | None:
        if value is None:
            return None
        source_parts = [part.strip() for part in str(value).strip().split("\\") if part.strip()]
        if len(source_parts) >= 3:
            return "-".join(source_parts[:-1])
        normalized = cls._normalize_inventory_sku(value)
        if not normalized:
            return None
        parts = normalized.split("-")
        return "-".join(parts[:-1]) if len(parts) >= 3 else normalized

    @staticmethod
    def _style_reference(sku_prefix: str | None) -> str | None:
        return sku_prefix.split("-", 1)[0] if sku_prefix else None

    @classmethod
    def _color_group(cls, color: str | None) -> str:
        normalized = cls._slug(color or "").replace("-", " ")
        for group, keywords in COLOR_GROUP_KEYWORDS:
            if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in keywords):
                return group
        return "Anden"

    @staticmethod
    def _plain_text(html: str) -> str | None:
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return text or None

    @staticmethod
    def _slug(value: str) -> str:
        value = value.translate(str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "Æ": "Ae", "Ø": "Oe", "Å": "Aa"}))
        ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")

    @staticmethod
    def _absolute_url(url: str) -> str:
        if url.startswith("//"):
            return f"https:{url}"
        return urljoin(BASE_URL, url)

    @classmethod
    def _images(cls, product: dict[str, Any]) -> list[str]:
        urls: list[str] = []
        for image in product.get("images", []):
            if isinstance(image, dict):
                image = image.get("src") or image.get("url")
            if isinstance(image, str) and image:
                urls.append(cls._absolute_url(image))
        return cls._deduplicate(urls)

    @staticmethod
    def _deduplicate(values: Iterable[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if value not in result:
                result.append(value)
        return result

    @staticmethod
    def _handle_from_url(url_or_handle: str) -> str:
        value = url_or_handle.split("?", 1)[0].rstrip("/")
        return value.rsplit("/products/", 1)[-1].rsplit("/", 1)[-1]

    @staticmethod
    def _empty_store(name: str) -> dict[str, Any]:
        return {"name": name, "stock_known": True, "available": False, "total_stock": 0, "sizes": {}}
