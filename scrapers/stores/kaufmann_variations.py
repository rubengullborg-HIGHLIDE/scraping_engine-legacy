from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


VARIATION_URL_TEMPLATE = "https://www.kaufmann.dk/widgets/product/variation/{source_parent_id}"

AARHUS_STORES = (
    {"seo_url": "bruuns-galleri", "label": "KAUFMANN Aarhus, Bruuns Galleri"},
    {"seo_url": "storcenter-nord", "label": "KAUFMANN Aarhus, Storcenter Nord"},
    {"seo_url": "aarhus-c", "label": "KAUFMANN Aarhus, Strøget - Regina"},
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
    "Accept": "application/json,text/plain,*/*",
}


class KaufmannVariationError(RuntimeError):
    """Base error for the public Kaufmann variation endpoint."""


class KaufmannVariationUnavailable(KaufmannVariationError):
    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(f"Kaufmann variation endpoint returned HTTP {status_code}: {url}")


class KaufmannVariationParseError(KaufmannVariationError):
    """The endpoint responded, but its payload was incomplete or malformed."""


def clean_product_url(url: str) -> str:
    split = urlsplit(url.split("#", 1)[0])
    return urlunsplit((split.scheme, split.netloc, split.path, "", ""))


def extract_source_parent_id(page_html: str) -> Optional[str]:
    decoded = html.unescape(page_html)
    patterns = (
        r"productStore\.setup\(\s*['\"]([0-9a-f]{32})['\"]",
        r'"parentId"\s*:\s*"([0-9a-f]{32})"',
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_price(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    normalized = str(value).replace("DKK", "").replace("kr.", "").replace("kr", "")
    normalized = normalized.replace("\xa0", " ").strip()
    match = re.search(r"\d[\d.\s]*(?:,\d{1,2})?", normalized)
    if not match:
        return None
    number = match.group(0).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def _integer(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _stock_by_store(size_option: dict[str, Any]) -> dict[str, dict[str, Any]]:
    stock = size_option.get("stock") or {}
    if not isinstance(stock, dict):
        return {}
    return {
        str(item.get("seoUrl")): item
        for item in stock.values()
        if isinstance(item, dict) and item.get("seoUrl")
    }


def variation_rows_from_payload(
    payload: dict[str, Any],
    source_parent_id: str,
    canonical_url: str,
    *,
    checked_at: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise KaufmannVariationParseError("Variation payload is not an object.")

    colors = (payload.get("options") or {}).get("color")
    if not isinstance(colors, dict) or not colors:
        raise KaufmannVariationParseError("Variation payload has no color options.")

    checked_at = checked_at or datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []

    for source_color_id, color_option in colors.items():
        if not isinstance(color_option, dict):
            raise KaufmannVariationParseError(
                f"Color option {source_color_id!r} is not an object."
            )
        source_available = color_option.get("available")
        if not isinstance(source_available, bool):
            raise KaufmannVariationParseError(
                f"Color option {source_color_id!r} has no boolean available flag."
            )

        sizes = color_option.get("sizes") or {}
        if not isinstance(sizes, dict):
            raise KaufmannVariationParseError(
                f"Color option {source_color_id!r} has malformed sizes."
            )

        inventory = {
            "stores": {
                store["seo_url"]: {
                    "name": store["label"],
                    "stock_known": True,
                    "available": False,
                    "total_stock": 0,
                    "sizes": {},
                }
                for store in AARHUS_STORES
            }
        }
        webshop_sizes: list[dict[str, Any]] = []

        for size_id, size_option in sizes.items():
            if not isinstance(size_option, dict):
                raise KaufmannVariationParseError(
                    f"Size option {size_id!r} for color {source_color_id!r} is malformed."
                )
            size_name = str(size_option.get("sizeName") or "").strip()
            if not size_name:
                continue

            webshop_stock = _integer(size_option.get("availability"))
            webshop_sizes.append(
                {
                    "size": size_name,
                    "in_stock": source_available and webshop_stock > 0,
                    "stock": webshop_stock,
                    "source_size_variant_id": size_option.get("productId") or size_id,
                    "source_product_number": size_option.get("productNumber"),
                }
            )

            source_stores = _stock_by_store(size_option)
            for store in AARHUS_STORES:
                store_key = store["seo_url"]
                source_store = source_stores.get(store_key) or {}
                stock = _integer(source_store.get("stock"))
                available = source_available and (
                    stock > 0 or bool(source_store.get("available"))
                )
                store_summary = inventory["stores"][store_key]
                store_summary["total_stock"] += stock
                store_summary["available"] = store_summary["available"] or available
                store_summary["sizes"][size_name] = {
                    "available": available,
                    "stock": stock,
                }

        if not source_available:
            for webshop_size in webshop_sizes:
                webshop_size["stock"] = 0
            for store_summary in inventory["stores"].values():
                store_summary["available"] = False
                store_summary["total_stock"] = 0
                for size_summary in store_summary["sizes"].values():
                    size_summary["available"] = False
                    size_summary["stock"] = 0

        aarhus_total_stock = sum(
            int(store.get("total_stock") or 0)
            for store in inventory["stores"].values()
        )
        current_price = parse_price(
            color_option.get("priceRaw")
            if color_option.get("priceRaw") is not None
            else color_option.get("price")
        )
        list_price = parse_price(
            color_option.get("listPriceRaw")
            if color_option.get("listPriceRaw") is not None
            else color_option.get("listPrice")
        )
        if current_price is not None and list_price is not None and list_price <= current_price:
            list_price = None

        rows.append(
            {
                "source_parent_id": source_parent_id,
                "source_color_id": str(source_color_id),
                "canonical_url": canonical_url,
                "current_price": current_price,
                "list_price": list_price,
                "webshop_sizes": webshop_sizes,
                "aarhus_inventory": inventory,
                "aarhus_total_stock": aarhus_total_stock,
                "aarhus_available": source_available
                and any(store["available"] for store in inventory["stores"].values()),
                "source_available": source_available,
                "publication_status": "active" if source_available else "unavailable",
                "status_reason": None if source_available else "source_available_false",
                "status_checked_at": checked_at,
                "discontinued_at": None if source_available else checked_at,
                "last_inventory_checked_at": checked_at,
                "consecutive_source_misses": 0,
                "last_refresh_error": None,
                "last_refresh_error_at": None,
                "scraped_at": checked_at,
                "updated_at": checked_at,
            }
        )

    return rows


class KaufmannVariationClient:
    def __init__(
        self,
        session: Optional[requests.Session] = None,
        *,
        timeout_seconds: float = 30,
        max_retries: int = 2,
    ) -> None:
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.timeout_seconds = timeout_seconds
        self.session.headers.update(DEFAULT_HEADERS)

        retry = Retry(
            total=max_retries,
            connect=max_retries,
            read=max_retries,
            status=max_retries,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def fetch_payload(self, source_parent_id: str) -> dict[str, Any]:
        url = VARIATION_URL_TEMPLATE.format(
            source_parent_id=quote(str(source_parent_id), safe="")
        )
        response = self.session.get(url, timeout=self.timeout_seconds)
        if response.status_code in {404, 410}:
            raise KaufmannVariationUnavailable(response.status_code, url)
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise KaufmannVariationParseError(
                f"Variation endpoint returned invalid JSON: {url}"
            ) from exc
        if not isinstance(payload, dict):
            raise KaufmannVariationParseError(
                f"Variation endpoint returned a non-object payload: {url}"
            )
        return payload

    def resolve_source_parent_id(self, product_url: str) -> str:
        canonical_url = clean_product_url(product_url)
        response = self.session.get(canonical_url, timeout=self.timeout_seconds)
        if response.status_code in {404, 410}:
            raise KaufmannVariationUnavailable(response.status_code, canonical_url)
        response.raise_for_status()
        source_parent_id = extract_source_parent_id(response.text)
        if not source_parent_id:
            raise KaufmannVariationParseError(
                f"Could not find Kaufmann source parent id in {canonical_url}"
            )
        return source_parent_id

    def close(self) -> None:
        if self._owns_session:
            self.session.close()
