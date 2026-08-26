"""Full-catalog importer for CEJF's men's Shopify collection."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://cejf.dk"
MEN_COLLECTION_HANDLE = "men"
AARHUS_STORE_SLUG = "cejf-aarhus"
AARHUS_STORE = {
    "name": "Ćejf Aarhus",
    "address": "Graven 3B, 8000 Aarhus C",
}

MATERIAL_WORDS = (
    "fabric|cotton|wool|denim|flannel|jacquard|twill|corduroy|boucl[eé]|"
    "poplin|oxford|polyester|polyamide|polyamid|viscose|elastane|elastan|"
    "linen|lyocell|silk|leather|suede"
)


class CejfScraper:
    """Read CEJF's small men's catalogue and published Shopify availability."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
            "Accept": "application/json,text/html,*/*",
        })

    def discover_products(self, collection_handle: str = MEN_COLLECTION_HANDLE) -> list[dict[str, Any]]:
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
                if product.get("id") is None or not self.is_mens_product(product):
                    continue
                product["_discovery_collections"] = [collection_handle]
                products[int(product["id"])] = product
            if len(batch) < 250:
                break
            page += 1
        return list(products.values())

    @classmethod
    def is_mens_product(cls, product: dict[str, Any]) -> bool:
        return "men" in {tag.casefold() for tag in cls._strings(product.get("tags"))}

    def fetch_product(self, url_or_handle: str) -> dict[str, Any]:
        handle = self._handle_from_url(url_or_handle)
        response = self.session.get(f"{BASE_URL}/products/{handle}.js", timeout=30)
        response.raise_for_status()
        return response.json()

    def product_to_row(self, product: dict[str, Any]) -> dict[str, Any]:
        product_id = str(product["id"])
        handle = self._text(product.get("handle"))
        if not handle:
            raise ValueError(f"CEJF product {product_id} has no handle.")
        canonical_url = f"{BASE_URL}/products/{handle}"
        name = self._text(product.get("title"))
        if not name:
            raise ValueError(f"CEJF product {product_id} has no title.")

        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        description = self._description(product.get("body_html") or product.get("description"))
        material = self._primary_material(description)
        fabric_description = self._fabric_description(description)
        specifications = {"fabric_description": fabric_description} if fabric_description else {}
        color = self._color(name, description)
        category = self._category(name)
        current_price = self._minimum_variant_price(variants, "price")
        list_price = self._minimum_sale_list_price(variants)
        scraped_at = datetime.now(timezone.utc).isoformat()

        webshop_sizes = []
        local_sizes: dict[str, dict[str, Any]] = {}
        aarhus_available = False
        for variant in variants:
            label = self._text(variant.get("public_title") or variant.get("title")) or str(variant["id"])
            size = self._variant_size(product, variant) or label
            price = self._source_price(variant.get("price"))
            compare_at = self._source_price(variant.get("compare_at_price"))
            if compare_at is not None and price is not None and compare_at <= price:
                compare_at = None
            available = bool(variant.get("available"))
            webshop_sizes.append({
                "label": label,
                "size": size,
                "in_stock": available,
                "stock": None,
                "stock_known": False,
                "current_price": price,
                "list_price": compare_at,
                "source_size_variant_id": str(variant["id"]),
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })
            local_sizes[label] = {
                "available": available,
                "stock": None,
                "size": size,
            }
            aarhus_available = aarhus_available or available

        local_inventory = {
            "stores": {
                AARHUS_STORE_SLUG: {
                    "name": AARHUS_STORE["name"],
                    "address": AARHUS_STORE["address"],
                    "stock_known": False,
                    "available": aarhus_available,
                    "total_stock": None,
                    "sizes": local_sizes,
                }
            }
        }
        category_path = ["Clothing", category] if category else ["Clothing"]
        return {
            "source_product_id": product_id,
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "name": name,
            "brand": self._text(product.get("vendor")) or "Ćejf",
            "color": color,
            "color_group": self._color_group(color),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": description,
            "specifications": specifications,
            "materials": [material] if material else [],
            "fit": self._fit(description),
            "country_of_origin": self._country_of_origin(description),
            "category": category,
            "category_path": category_path,
            "tags": self._strings(product.get("tags")),
            "images": self._images(product),
            "webshop_sizes": webshop_sizes,
            "local_inventory": local_inventory,
            "local_total_stock": None,
            "local_available": aarhus_available,
            "aarhus_total_stock": None,
            "aarhus_available": aarhus_available,
            "inventory_checked_at": scraped_at,
            "raw": {
                "shopify_product_id": product_id,
                "shopify_handle": handle,
                "shopify_product_type": self._text(product.get("type") or product.get("product_type")),
                "shopify_options": product.get("options") or [],
                "variant_skus": {
                    str(variant["id"]): variant.get("sku")
                    for variant in variants
                },
                "discovery_collections": product.get("_discovery_collections") or [],
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
                "source_updated_at": product.get("updated_at"),
                "local_inventory_status": "webshop_boolean_used_as_single_store_proxy",
                "local_inventory_basis": (
                    "CEJF has one physical shop; exact counts are not published, and Shopify's "
                    "webshop available flag is used only as a boolean Aarhus availability proxy."
                ),
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _description(cls, value: Any) -> str | None:
        if not value:
            return None
        text = BeautifulSoup(str(value), "html.parser").get_text("\n")
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return "\n\n".join(cls._dedupe(line for line in lines if line)) or None

    @staticmethod
    def _sentences(text: str | None) -> list[str]:
        if not text:
            return []
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.replace("\n", " ")) if part.strip()]

    @classmethod
    def _fabric_description(cls, description: str | None) -> str | None:
        for sentence in cls._sentences(description):
            if re.search(rf"\b(?:{MATERIAL_WORDS})\b", sentence, flags=re.IGNORECASE):
                return sentence
        return None

    @classmethod
    def _primary_material(cls, description: str | None) -> str | None:
        sentence = cls._fabric_description(description)
        if not sentence:
            return None
        patterns = (
            r"\b(?:made|crafted) from\s+(.+?)(?=,\s+(?:this|these)\b|[.;]|$)",
            r"\bcrafted from\s+(.+?)(?=[.;]|$)",
            rf"\bmade in\s+((?:an?\s+)?[^.;,]*\b(?:{MATERIAL_WORDS})\b)(?=\s+with\b|[.;,]|$)",
            r"\bcrafted from\s+(.+?)(?=[.;]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, sentence, flags=re.IGNORECASE)
            if match:
                return cls._clean_material(match.group(1))

        soft_flannel = re.match(r"Soft,\s*(.+?)(?=\s+with\b|,|\.)", sentence, flags=re.IGNORECASE)
        if soft_flannel:
            return cls._clean_material(soft_flannel.group(1))

        leading_material = re.match(
            rf"^(.+?\b(?:{MATERIAL_WORDS})\b)(?=\s+(?:in|with|from)\b|,|\.|$)",
            sentence,
            flags=re.IGNORECASE,
        )
        if leading_material:
            return cls._clean_material(leading_material.group(1))

        clause = sentence.split(",", 1)[0].strip()
        if re.search(rf"\b(?:{MATERIAL_WORDS})\b", clause, flags=re.IGNORECASE):
            return cls._clean_material(clause)
        return None

    @staticmethod
    def _clean_material(value: str) -> str:
        return re.sub(r"^(?:a|an)\s+", "", re.sub(r"\s+", " ", value).strip(" ."), flags=re.IGNORECASE)

    @classmethod
    def _fit(cls, description: str | None) -> str | None:
        text = " ".join(cls._sentences(description))
        rules = (
            (r"relaxed,?\s+oversized fit", "Relaxed, oversized fit"),
            (r"classic fit", "Classic fit"),
            (r"fitted but not slim[^.]*true to size", "Fitted but not slim; true to size"),
            (r"true to size", "True to size"),
            (r"relaxed,?\s+(?:versatile\s+)?silhouette", "Relaxed silhouette"),
            (r"relaxed suit trousers", "Relaxed fit"),
        )
        for pattern, value in rules:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return value
        return None

    @staticmethod
    def _country_of_origin(description: str | None) -> str | None:
        if not description:
            return None
        for sentence in reversed(CejfScraper._sentences(description)):
            match = re.fullmatch(r"Made in\s+([A-Za-z ]{2,40})[.]?", sentence, flags=re.IGNORECASE)
            if not match:
                continue
            country = re.sub(r"\s+", " ", match.group(1)).strip()
            if not re.search(rf"\b(?:{MATERIAL_WORDS})\b", country, flags=re.IGNORECASE):
                return country
        return None

    @staticmethod
    def _category(name: str) -> str | None:
        normalized = name.casefold()
        if "pant" in normalized or "trouser" in normalized:
            return "Pants"
        if "jacket" in normalized:
            return "Jackets"
        if "overshirt" in normalized:
            return "Overshirts"
        if "shirt" in normalized:
            return "Shirts"
        return None

    @classmethod
    def _color(cls, name: str, description: str | None) -> str | None:
        colors = (
            "Grey Brown", "Grey Melange", "Blue Stripe", "Blue Check", "Light Blue",
            "Dark Denim", "Off White", "Black", "Navy", "Indigo", "Grey", "Gray",
            "Brown", "Blue", "Green", "White", "Cream", "Purple", "Red", "Mix",
        )
        for source in (name, description or ""):
            matches = []
            for color in colors:
                match = re.search(rf"\b{re.escape(color)}\b", source, flags=re.IGNORECASE)
                if match:
                    matches.append((match.start(), -len(color), color))
            if matches:
                return min(matches)[2].replace("Gray", "Grey")
        return None

    @staticmethod
    def _color_group(color: str | None) -> str | None:
        if not color:
            return None
        normalized = color.casefold()
        if normalized in {"mix", "grey brown"}:
            return "Multicolor"
        groups = (
            ("Black", ("black",)),
            ("White", ("white", "cream")),
            ("Grey", ("grey", "gray")),
            ("Blue", ("blue", "navy", "indigo", "denim")),
            ("Green", ("green",)),
            ("Brown", ("brown",)),
            ("Red", ("red",)),
            ("Purple", ("purple",)),
        )
        for group, values in groups:
            if any(value in normalized for value in values):
                return group
        return None

    @classmethod
    def _variant_size(cls, product: dict[str, Any], variant: dict[str, Any]) -> str | None:
        definitions = product.get("options") or []
        names = [cls._text(option.get("name") if isinstance(option, dict) else option) for option in definitions]
        values = variant.get("options") or [variant.get("option1"), variant.get("option2"), variant.get("option3")]
        for name, value in zip(names, values):
            if str(name or "").casefold() in {"size", "størrelse", "storrelse"}:
                return cls._text(value)
        return cls._text(variant.get("public_title") or variant.get("title"))

    @classmethod
    def _minimum_variant_price(cls, variants: list[dict[str, Any]], key: str) -> float | None:
        values = [cls._source_price(variant.get(key)) for variant in variants]
        values = [value for value in values if value is not None]
        return min(values) if values else None

    @classmethod
    def _minimum_sale_list_price(cls, variants: list[dict[str, Any]]) -> float | None:
        sale_values = []
        for variant in variants:
            price = cls._source_price(variant.get("price"))
            compare_at = cls._source_price(variant.get("compare_at_price"))
            if price is not None and compare_at is not None and compare_at > price:
                sale_values.append(compare_at)
        return min(sale_values) if sale_values else None

    @staticmethod
    def _source_price(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price / 100 if isinstance(value, int) else price

    @classmethod
    def _images(cls, product: dict[str, Any]) -> list[str]:
        images = []
        for value in product.get("images") or []:
            source = value.get("src") if isinstance(value, dict) else value
            if source:
                images.append(urljoin(BASE_URL, str(source)))
        return cls._dedupe(images)

    @staticmethod
    def _handle_from_url(url_or_handle: str) -> str:
        value = str(url_or_handle).strip()
        if "/products/" in value:
            value = urlparse(value).path.split("/products/", 1)[1]
        return value.split("?", 1)[0].strip("/").removesuffix(".js")

    @staticmethod
    def _text(value: Any) -> str | None:
        text = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
        return text or None

    @classmethod
    def _strings(cls, values: Any) -> list[str]:
        if values is None:
            return []
        if isinstance(values, str):
            values = values.split(",")
        return cls._dedupe(cls._text(value) for value in values if cls._text(value))

    @staticmethod
    def _dedupe(values: Iterable[Any]) -> list[Any]:
        result: list[Any] = []
        seen: set[str] = set()
        for value in values:
            if value is None:
                continue
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(value)
        return result
