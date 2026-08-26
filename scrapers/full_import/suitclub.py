"""Full-catalog importer for SuitClub's men's clothing and footwear."""

from __future__ import annotations

import html as html_module
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://suitclub.dk"
PARTS_COLLECTION_HANDLE = "enkelte-dele"
DEFAULT_COLLECTION_HANDLES = (PARTS_COLLECTION_HANDLE, "toej", "sko-til-jakkesaet")
ALLOWED_PRODUCT_TYPES = frozenset({
    "Blazer",
    "Suit pants",
    "Veste",
    "Skjorter",
    "Strik",
    "T-shirt",
    "Sko",
})
CATEGORY_PATHS = {
    "Blazer": ["Clothing", "Suits", "Blazers"],
    "Suit pants": ["Clothing", "Suits", "Trousers"],
    "Veste": ["Clothing", "Suits", "Vests"],
    "Skjorter": ["Clothing", "Shirts"],
    "Strik": ["Clothing", "Knitwear"],
    "T-shirt": ["Clothing", "T-shirts"],
    "Sko": ["Footwear", "Shoes"],
}
ONLINE_WAREHOUSE_SOURCE_NAME = "Lager Aarhus"
TRACKED_STORES = {
    "suitclub-aarhus": {
        "name": "SuitClub Aarhus",
        "source_name": "Butik Aarhus",
        "address": "Guldsmedgade 42, 8000 Aarhus C",
    },
    "suitclub-copenhagen": {
        "name": "SuitClub Copenhagen",
        "source_name": "Butik København",
        "address": "Bredgade 21, 1260 København K",
    },
    "suitclub-odense": {
        "name": "SuitClub Odense",
        "source_name": "Butik Odense",
        "address": "Kongensgade 2, 5000 Odense C",
    },
    "suitclub-aalborg": {
        "name": "SuitClub Aalborg",
        "source_name": "Butik Aalborg",
        "address": "Slotsgade 2, 9000 Aalborg",
    },
}


class SuitClubScraper:
    """Read SuitClub's Shopify catalogue, metadata, and exact location stock."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)",
            "Accept-Language": "da-DK,da;q=0.9",
            "Accept": "application/json,text/html,*/*",
        })

    def discover_products(
        self,
        collection_handles: Iterable[str] | str = DEFAULT_COLLECTION_HANDLES,
    ) -> list[dict[str, Any]]:
        """Discover and deduplicate SuitClub clothing and footwear products."""
        if isinstance(collection_handles, str):
            collection_handles = (collection_handles,)
        products: dict[int, dict[str, Any]] = {}
        for collection_handle in self._dedupe(collection_handles):
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
                    if product.get("id") is None or not self.is_catalog_product(product):
                        continue
                    product_id = int(product["id"])
                    existing = products.get(product_id)
                    if existing:
                        existing["_discovery_collections"] = self._dedupe([
                            *(existing.get("_discovery_collections") or []),
                            collection_handle,
                        ])
                    else:
                        product["_discovery_collections"] = [collection_handle]
                        products[product_id] = product
                if len(batch) < 250:
                    break
                page += 1
        return list(products.values())

    @staticmethod
    def is_catalog_product(product: dict[str, Any]) -> bool:
        return (product.get("type") or product.get("product_type")) in ALLOWED_PRODUCT_TYPES

    # Kept as a compatibility alias for callers from the original suit-parts importer.
    is_atomic_part = is_catalog_product

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

    def fetch_inventory_snapshot(
        self,
        product_ids: Iterable[int | str],
        config_page_html: str,
        *,
        batch_size: int = 10,
        batch_delay: float = 0.5,
    ) -> dict[str, Any]:
        """Fetch exact inventory for unique Shopify products through the public Storefront API."""
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        endpoint, token = self._storefront_config(config_page_html)
        locations_query = """
          query SuitClubLocations {
            locations(first: 10) {
              nodes {
                id
                name
                address { address1 address2 city zip country }
              }
            }
          }
        """
        location_payload = self._graphql(endpoint, token, locations_query)
        locations = ((location_payload.get("data") or {}).get("locations") or {}).get("nodes") or []

        inventory_query = """
          query SuitClubInventory($ids: [ID!]!) {
            nodes(ids: $ids) {
              ... on Product {
                id
                title
                productType
                variants(first: 100) {
                  nodes {
                    id
                    title
                    sku
                    availableForSale
                    quantityAvailable
                    selectedOptions { name value }
                    storeAvailability(first: 10) {
                      nodes {
                        available
                        quantityAvailable
                        location { id name }
                      }
                    }
                  }
                }
              }
            }
          }
        """
        ids = self._dedupe(str(product_id).rsplit("/", 1)[-1] for product_id in product_ids)
        products: dict[str, dict[str, Any] | None] = {}
        for offset in range(0, len(ids), batch_size):
            batch = ids[offset : offset + batch_size]
            for requested_id in batch:
                products[requested_id] = None
            variables = {"ids": [f"gid://shopify/Product/{product_id}" for product_id in batch]}
            payload = self._graphql(endpoint, token, inventory_query, variables)
            nodes = ((payload.get("data") or {}).get("nodes") or [])
            for requested_id, node in zip(batch, nodes):
                products[requested_id] = node
            if offset + batch_size < len(ids) and batch_delay > 0:
                time.sleep(batch_delay)

        version_match = re.search(r"/api/(\d{4}-\d{2})/graphql\.json", endpoint)
        return {
            "endpoint": endpoint,
            "api_version": version_match.group(1) if version_match else None,
            "locations": locations,
            "products": products,
        }

    def _graphql(
        self,
        endpoint: str,
        token: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Storefront-Access-Token": token,
        }
        for attempt in range(3):
            response = self.session.post(
                endpoint,
                headers=headers,
                json={"query": query, "variables": variables or {}},
                timeout=45,
            )
            response.raise_for_status()
            payload = response.json()
            errors = payload.get("errors") or []
            if not errors:
                return payload
            throttled = any((error.get("extensions") or {}).get("code") == "THROTTLED" for error in errors)
            if throttled and attempt < 2:
                time.sleep(attempt + 1)
                continue
            raise RuntimeError(f"SuitClub Storefront API error: {json.dumps(errors, ensure_ascii=False)[:1500]}")
        raise RuntimeError("SuitClub Storefront API remained throttled after retries.")

    def product_to_row(
        self,
        product: dict[str, Any],
        page_html: str,
        inventory_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Build one row for one independently purchasable SuitClub product/colour."""
        details = self._page_details(page_html)
        page_product = details.pop("page_product")
        product_id = str(page_product.get("id") or product["id"])
        handle = page_product.get("handle") or product.get("handle") or self._handle_from_url(product_id)
        canonical_url = f"{BASE_URL}/products/{handle}"
        product_type = page_product.get("type") or product.get("type") or product.get("product_type")
        variants = [variant for variant in product.get("variants", []) if variant.get("id") is not None]
        inventory_node = (inventory_snapshot.get("products") or {}).get(product_id)
        inventory_result = self._inventory_from_snapshot(
            product,
            variants,
            inventory_node,
            inventory_snapshot.get("locations") or [],
        )
        inventory = inventory_result["local_inventory"]
        webshop_sizes = inventory_result["webshop_sizes"]
        stores = inventory["stores"]
        all_local_exact = all(store.get("stock_known") for store in stores.values())
        local_total_stock = (
            sum(int(store["total_stock"]) for store in stores.values())
            if all_local_exact else None
        )
        local_available = (
            any(bool(store.get("available")) for store in stores.values())
            if inventory_node is not None else None
        )
        aarhus = stores["suitclub-aarhus"]
        aarhus_total_stock = aarhus.get("total_stock") if aarhus.get("stock_known") else None
        aarhus_available = bool(aarhus.get("available")) if inventory_node is not None else None

        current_price = self._price(page_product.get("price"), cents=True)
        if current_price is None:
            current_price = self._variant_price(variants, "price")
        list_price = self._price(page_product.get("compare_at_price"), cents=True)
        if list_price is None:
            list_price = self._variant_price(variants, "compare_at_price")
        if list_price is not None and current_price is not None and list_price <= current_price:
            list_price = None

        fit_options = self._option_values(product, "pasform", "fit")
        fit = " / ".join(fit_options) if fit_options else details["fit_guidance"]
        color = details["color"]
        scraped_at = datetime.now(timezone.utc).isoformat()
        source_metafields = (((page_product.get("metafields") or {}).get("custom")) or {})
        category_path = list(CATEGORY_PATHS.get(product_type, []))
        return {
            "source_product_id": product_id,
            "source_url": canonical_url,
            "canonical_url": canonical_url,
            "name": page_product.get("title") or product.get("title") or None,
            "brand": page_product.get("vendor") or product.get("vendor") or None,
            "product_type": product_type,
            "color": color,
            "color_group": self._color_group(color),
            "current_price": current_price,
            "list_price": list_price,
            "currency": "DKK",
            "description": details["description"],
            "highlights": details["highlights"],
            "specifications": details["specifications"],
            "materials": details["materials"],
            "fit": fit,
            "category": category_path[-1] if category_path else product_type,
            "category_path": category_path or ([product_type] if product_type else []),
            "tags": self._strings(page_product.get("tags") or product.get("tags")),
            "collections": details["collections"],
            "images": self._images(product, page_product),
            "model_info": details["model_info"],
            "matching_products": details["matching_products"],
            "related_colors": details["related_colors"],
            "webshop_sizes": webshop_sizes,
            "local_inventory": inventory,
            "local_total_stock": local_total_stock,
            "local_available": local_available,
            "aarhus_total_stock": aarhus_total_stock,
            "aarhus_available": aarhus_available,
            "inventory_checked_at": scraped_at,
            "raw": {
                "shopify_product_id": product_id,
                "shopify_handle": handle,
                "shopify_product_type": product_type,
                "shopify_category": page_product.get("category"),
                "shopify_options": product.get("options") or [],
                "variant_skus": {
                    str(variant["id"]): variant.get("sku")
                    for variant in variants
                },
                "discovery_collections": product.get("_discovery_collections") or [],
                "storefront_api_version": inventory_snapshot.get("api_version"),
                "storefront_locations": inventory_result["source_locations"],
                "storefront_product_found": inventory_node is not None,
                "source_metafields": source_metafields,
                "published_at": product.get("published_at"),
                "created_at": product.get("created_at"),
            },
            "scraped_at": scraped_at,
            "updated_at": scraped_at,
        }

    @classmethod
    def _page_details(cls, page_html: str) -> dict[str, Any]:
        soup = BeautifulSoup(page_html, "html.parser")
        node = soup.select_one("#stape-product-data")
        if not node:
            raise ValueError("SuitClub page is missing #stape-product-data.")
        try:
            page_product = json.loads(node.get_text())
        except json.JSONDecodeError as error:
            raise ValueError("SuitClub product metadata is not valid JSON.") from error

        custom = (((page_product.get("metafields") or {}).get("custom")) or {})
        sections = cls._description_sections(custom.get("beskrivelse"))
        material_details = sections["materials"]
        materials = cls._dedupe(line for line in material_details if re.search(r"\d+(?:[.,]\d+)?\s*%", line))
        size_lines = sections["size_and_fit"]
        fit_guidance = next(
            (line for line in size_lines if re.search(r"\bfit\b|pasform", line, flags=re.IGNORECASE)),
            None,
        )
        model_text = cls._text(custom.get("model")) or " ".join(
            line for line in size_lines if re.search(r"\d{2,3}\s*(?:cm|kg)|iført|bruger", line, flags=re.IGNORECASE)
        )
        model_info = cls._model_info(model_text)

        specifications: dict[str, Any] = {
            "collection": custom.get("kollektion"),
            "collection_types": cls._strings(custom.get("kollektions_type")),
            "pattern": custom.get("m_nster"),
            "subtitle": custom.get("sub_title"),
            "fabric_type": custom.get("stoftype"),
            "fabric_weight": custom.get("vaegt"),
            "care_instructions": custom.get("vaskeanvisning"),
            "material_details": material_details,
            "fit_guidance": fit_guidance,
        }
        source_category = cls._text(page_product.get("category"))
        if source_category and source_category.casefold() != "ikke kategoriseret":
            specifications["source_category"] = source_category
        specifications = {key: value for key, value in specifications.items() if value}
        return {
            "page_product": page_product,
            "description": "\n\n".join(sections["intro"]) or None,
            "highlights": sections["details"],
            "materials": materials,
            "fit_guidance": fit_guidance,
            "model_info": model_info,
            "color": cls._text(custom.get("farve")),
            "collections": cls._collections(page_product.get("collections")),
            "matching_products": cls._matching_products(custom),
            "related_colors": cls._related_colors(custom.get("color"), page_product.get("id")),
            "specifications": specifications,
        }

    @classmethod
    def _description_sections(cls, value: Any) -> dict[str, list[str]]:
        raw = html_module.unescape(html_module.unescape(str(value or "")))
        text = BeautifulSoup(raw, "html.parser").get_text("\n")
        sections = {"intro": [], "materials": [], "details": [], "size_and_fit": []}
        current = "intro"
        headings = {
            "materiale": "materials",
            "materialer": "materials",
            "detaljer": "details",
            "størrelse og pasform": "size_and_fit",
            "storrelse og pasform": "size_and_fit",
        }
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" \t:-")
            if not line:
                continue
            heading = re.sub(r"[^a-zæøå0-9 ]+", "", line.casefold()).strip()
            if heading in headings:
                current = headings[heading]
                continue
            sections[current].append(line)
        for key, lines in sections.items():
            sections[key] = cls._dedupe(lines)
        return sections

    @classmethod
    def _model_info(cls, value: Any) -> dict[str, Any]:
        text = cls._text(value)
        if not text:
            return {}
        result: dict[str, Any] = {"text": text}
        height = re.search(r"\b(\d{3})\s*cm\b", text, flags=re.IGNORECASE)
        weight = re.search(r"\b(\d{2,3})\s*kg\b", text, flags=re.IGNORECASE)
        worn = re.search(r"(?:bruger|iført(?:\s+en)?)\s+(?:størrelse\s+)?(.+)$", text, flags=re.IGNORECASE)
        if height:
            result["height_cm"] = int(height.group(1))
        if weight:
            result["weight_kg"] = int(weight.group(1))
        if worn:
            result["worn_size"] = worn.group(1).strip(" .")
        return result

    @classmethod
    def _matching_products(cls, custom: dict[str, Any]) -> dict[str, Any]:
        matching: dict[str, Any] = {}
        for role, source_key in (("blazer", "blazer"), ("pants", "suit_pants"), ("vest", "vest")):
            item = custom.get(source_key)
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            handle = cls._text(item.get("handle"))
            matching[role] = {
                "source_product_id": str(item["id"]),
                "title": cls._text(item.get("title")),
                "handle": handle,
                "url": f"{BASE_URL}/products/{handle}" if handle else None,
            }
        return matching

    @classmethod
    def _related_colors(cls, values: Any, current_product_id: Any) -> list[dict[str, Any]]:
        related: list[dict[str, Any]] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            handle = cls._text(item.get("handle"))
            related.append({
                "source_product_id": str(item["id"]),
                "title": cls._text(item.get("title")),
                "handle": handle,
                "url": f"{BASE_URL}/products/{handle}" if handle else None,
                "current": str(item["id"]) == str(current_product_id),
            })
        return related

    @classmethod
    def _collections(cls, values: Any) -> list[dict[str, Any]]:
        collections: list[dict[str, Any]] = []
        for item in values if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            collection = {
                "source_collection_id": str(item["id"]) if item.get("id") is not None else None,
                "title": cls._text(item.get("title")),
                "handle": cls._text(item.get("handle")),
            }
            collections.append(collection)
        return collections

    @classmethod
    def _inventory_from_snapshot(
        cls,
        product: dict[str, Any],
        variants: list[dict[str, Any]],
        inventory_node: dict[str, Any] | None,
        locations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        location_by_id = {str(location.get("id")): location for location in locations if location.get("id")}
        physical_location_ids: dict[str, str] = {}
        online_location_id = None
        source_locations: list[dict[str, Any]] = []
        for location in locations:
            location_id = str(location.get("id") or "")
            source_name = cls._text(location.get("name"))
            slug = cls._store_slug(source_name)
            if slug:
                physical_location_ids[slug] = location_id
            if source_name == ONLINE_WAREHOUSE_SOURCE_NAME:
                online_location_id = location_id
            source_locations.append({
                "id": location_id or None,
                "name": source_name,
                "address": location.get("address") or {},
                "tracked_store": slug,
                "online_warehouse": source_name == ONLINE_WAREHOUSE_SOURCE_NAME,
            })

        exact_product = inventory_node is not None
        stores: dict[str, dict[str, Any]] = {}
        for slug, config in TRACKED_STORES.items():
            location = location_by_id.get(physical_location_ids.get(slug, ""), {})
            stores[slug] = {
                "name": config["name"],
                "address": cls._address(location) or config["address"],
                "stock_known": exact_product and slug in physical_location_ids,
                "available": False,
                "total_stock": 0 if exact_product and slug in physical_location_ids else None,
                "sizes": {},
            }

        inventory_variants = {
            str(node.get("id") or "").rsplit("/", 1)[-1]: node
            for node in (((inventory_node or {}).get("variants") or {}).get("nodes") or [])
            if node.get("id")
        }
        webshop_sizes: list[dict[str, Any]] = []
        for variant in variants:
            variant_id = str(variant["id"])
            label = cls._text(variant.get("public_title") or variant.get("title")) or variant_id
            size, fit = cls._variant_options(product, variant)
            inventory_variant = inventory_variants.get(variant_id)
            availability_nodes = (
                (((inventory_variant or {}).get("storeAvailability") or {}).get("nodes") or [])
                if inventory_variant else []
            )
            availability_by_location = {
                str((item.get("location") or {}).get("id")): item
                for item in availability_nodes
                if (item.get("location") or {}).get("id")
            }

            local_stores: list[str] = []
            known_local_stock = 0
            local_stock_known = inventory_variant is not None
            for slug, store in stores.items():
                location_id = physical_location_ids.get(slug)
                state = availability_by_location.get(location_id or "")
                stock = cls._integer(state.get("quantityAvailable")) if state is not None else None
                known = inventory_variant is not None and location_id is not None and stock is not None
                available = bool(stock and stock > 0) if known else bool((state or {}).get("available"))
                store["sizes"][label] = {
                    "available": available,
                    "stock": stock if known else None,
                    "size": size,
                    "fit": fit,
                }
                store["available"] = bool(store["available"] or available)
                if known and isinstance(store.get("total_stock"), int):
                    store["total_stock"] += stock
                else:
                    store["stock_known"] = False
                    store["total_stock"] = None
                    local_stock_known = False
                if available:
                    local_stores.append(slug)
                if known:
                    known_local_stock += stock

            online_state = availability_by_location.get(online_location_id or "")
            online_stock = cls._integer(online_state.get("quantityAvailable")) if online_state is not None else None
            online_known = inventory_variant is not None and online_location_id is not None and online_stock is not None
            webshop_sizes.append({
                "label": label,
                "size": size,
                "fit": fit,
                "in_stock": bool(online_stock and online_stock > 0) if online_known else None,
                "stock": online_stock if online_known else None,
                "stock_known": online_known,
                "local_available": bool(local_stores),
                "local_stores": local_stores,
                "local_stock": known_local_stock if local_stock_known else None,
                "local_stock_known": local_stock_known,
                "source_size_variant_id": variant_id,
                "source_product_number": variant.get("sku") or variant.get("barcode") or None,
            })

        return {
            "local_inventory": {"stores": stores},
            "webshop_sizes": webshop_sizes,
            "source_locations": source_locations,
        }

    @staticmethod
    def _storefront_config(page_html: str) -> tuple[str, str]:
        endpoint_match = re.search(
            r"https://[A-Za-z0-9.-]+/api/\d{4}-\d{2}/graphql\.json",
            page_html,
        )
        token_match = re.search(
            r"X-Shopify-Storefront-Access-Token['\"]?\s*:\s*['\"]([^'\"]+)",
            page_html,
            flags=re.IGNORECASE,
        )
        if not endpoint_match or not token_match:
            raise ValueError("Could not discover SuitClub's public Storefront API configuration.")
        return endpoint_match.group(0), token_match.group(1)

    @staticmethod
    def _store_slug(source_name: str | None) -> str | None:
        normalized = str(source_name or "").strip().casefold()
        for slug, config in TRACKED_STORES.items():
            if normalized == config["source_name"].casefold():
                return slug
        return None

    @classmethod
    def _variant_options(cls, product: dict[str, Any], variant: dict[str, Any]) -> tuple[str | None, str | None]:
        definitions = product.get("options") or []
        names = [
            cls._text(option.get("name") if isinstance(option, dict) else option)
            for option in definitions
        ]
        values = variant.get("options") or [variant.get("option1"), variant.get("option2"), variant.get("option3")]
        size = None
        fit = None
        for name, value in zip(names, values):
            normalized = str(name or "").casefold()
            if normalized in {"størrelse", "storrelse", "size"}:
                size = cls._text(value)
            if normalized in {"pasform", "fit"}:
                fit = cls._text(value)
        if not size:
            size = cls._text(variant.get("public_title") or variant.get("title"))
        return size, fit

    @classmethod
    def _option_values(cls, product: dict[str, Any], *wanted_names: str) -> list[str]:
        wanted = {name.casefold() for name in wanted_names}
        for index, option in enumerate(product.get("options") or []):
            if isinstance(option, dict):
                name = str(option.get("name") or "").casefold()
                if name in wanted:
                    return cls._strings(option.get("values"))
                continue
            if str(option or "").casefold() in wanted:
                values = []
                for variant in product.get("variants") or []:
                    options = variant.get("options") or [
                        variant.get("option1"),
                        variant.get("option2"),
                        variant.get("option3"),
                    ]
                    if index < len(options):
                        values.append(options[index])
                return cls._strings(values)
        return []

    @staticmethod
    def _address(location: dict[str, Any]) -> str | None:
        address = location.get("address") or {}
        first = " ".join(filter(None, [address.get("address1"), address.get("address2")])).strip()
        second = " ".join(filter(None, [address.get("zip"), address.get("city")])).strip()
        value = ", ".join(part for part in (first, second) if part)
        return value or None

    @staticmethod
    def _integer(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _price(value: Any, *, cents: bool = False) -> float | None:
        if value is None or value == "":
            return None
        try:
            price = float(value)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        return price / 100 if cents else price

    @classmethod
    def _variant_price(cls, variants: list[dict[str, Any]], key: str) -> float | None:
        prices = [cls._price(variant.get(key)) for variant in variants]
        prices = [price for price in prices if price is not None]
        return min(prices) if prices else None

    @classmethod
    def _images(cls, product: dict[str, Any], page_product: dict[str, Any]) -> list[str]:
        images: list[str] = []
        for value in product.get("images") or []:
            source = value.get("src") if isinstance(value, dict) else value
            if source:
                images.append(urljoin(BASE_URL, str(source)))
        custom = (((page_product.get("metafields") or {}).get("custom")) or {})
        for key in ("packshot", "color_image", "stofbillede"):
            if custom.get(key):
                images.append(urljoin(BASE_URL, str(custom[key])))
        return cls._dedupe(images)

    @staticmethod
    def _color_group(color: str | None) -> str | None:
        if not color:
            return None
        normalized = color.casefold()
        groups = (
            ("Black", ("black", "sort")),
            ("White", ("white", "hvid", "off-white", "cream", "ivory")),
            ("Grey", ("grey", "gray", "grå", "gra", "silver")),
            ("Blue", ("blue", "blå", "bla", "navy")),
            ("Green", ("green", "grøn", "gron", "olive")),
            ("Brown", ("brown", "brun", "beige", "sand", "stone", "tan")),
            ("Red", ("red", "rød", "rod", "burgundy")),
            ("Yellow", ("yellow", "gul", "gold")),
            ("Orange", ("orange",)),
            ("Purple", ("purple", "lilla", "violet")),
            ("Pink", ("pink", "rosa")),
        )
        for group, values in groups:
            if any(value in normalized for value in values):
                return group
        return None

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
