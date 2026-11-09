"""Helpers shared by the Shopify storefront scrapers.

New Shopify stores build on this module instead of copying another store's
private helpers. It understands both Shopify product formats:

- ``/products/<handle>.js``: prices are integer øre, ``type``, images are
  protocol-relative strings, options are objects with ``name``/``values``.
- ``/collections/<handle>/products.json``: prices are kroner strings,
  ``product_type``, images are objects with ``src``.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from scrapers.source_http import SourcePayloadError

Price = Optional[float]

USER_AGENT = "Mozilla/5.0 (compatible; HIGHLIDE catalog importer/1.0; +https://highlide.dk)"
PAGE_SIZE = 250
MAX_COLLECTION_PAGES = 60


class ShopifyStorefront:
    """Read-only access to one Shopify storefront's public endpoints."""

    def __init__(self, base_url: str, session: requests.Session | None = None, *, timeout: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
            "Accept": "application/json,text/html,*/*",
        })

    def collection_products(
        self,
        handle: str,
        keep: Callable[[dict[str, Any]], bool] = lambda product: True,
    ) -> list[dict[str, Any]]:
        """Every product of a collection feed, deduplicated by id.

        Stops on a short page; raises on an empty first page or when Shopify
        repeats a page, so a clamped ``page`` parameter can never loop forever
        or pass off a broken feed as an empty catalogue.
        """
        products: dict[int, dict[str, Any]] = {}
        previous_ids: Optional[list[int]] = None
        for page in range(1, MAX_COLLECTION_PAGES + 1):
            response = self.session.get(
                f"{self.base_url}/collections/{handle}/products.json",
                params={"limit": PAGE_SIZE, "page": page},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("products") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                raise SourcePayloadError(f"Invalid collection payload for {handle!r} page {page}")
            if not batch:
                if page == 1:
                    raise SourcePayloadError(f"Collection {handle!r} returned no products; verify discovery")
                break
            ids = [int(product["id"]) for product in batch if product.get("id") is not None]
            if ids == previous_ids:
                raise SourcePayloadError(f"Collection {handle!r} repeated page {page}; pagination is not advancing")
            previous_ids = ids
            for product in batch:
                if product.get("id") is None or not keep(product):
                    continue
                stored = products.setdefault(int(product["id"]), product)
                stored.setdefault("_discovery_collections", [])
                if handle not in stored["_discovery_collections"]:
                    stored["_discovery_collections"].append(handle)
            if len(batch) < PAGE_SIZE:
                break
        else:
            raise SourcePayloadError(f"Collection {handle!r} exceeded {MAX_COLLECTION_PAGES} pages")
        return list(products.values())

    def product_js(self, url_or_handle: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}/products/{handle_from_url(url_or_handle)}.js", timeout=self.timeout)
        response.raise_for_status()
        product = response.json()
        if not isinstance(product, dict) or product.get("id") is None:
            raise SourcePayloadError(f"Invalid product payload for {url_or_handle!r}")
        return product

    def variant_section(self, variant_id: Any, section_id: str) -> str:
        response = self.session.get(
            f"{self.base_url}/variants/{variant_id}/", params={"section_id": section_id}, timeout=self.timeout)
        response.raise_for_status()
        return response.text

    def canonical_url(self, handle: str) -> str:
        return f"{self.base_url}/products/{handle}"

    def close(self) -> None:
        self.session.close()


def handle_from_url(url_or_handle: str) -> str:
    value = str(url_or_handle).strip()
    if "/products/" in value:
        value = urlparse(value).path.rsplit("/products/", 1)[-1]
    value = value.split("?", 1)[0].split("#", 1)[0].strip("/")
    return value[:-3] if value.endswith(".js") else value


def money(value: Any) -> Price:
    """Price in kroner from either feed: integer øre (.js) or kroner strings."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return round(amount / 100, 2) if isinstance(value, int) else amount


def variant_prices(
    variants: Iterable[dict[str, Any]],
    price_of: Callable[[Any], Price] = money,
) -> tuple[Price, Price]:
    """Return (current_price, list_price) for a colour's size variants.

    The current price is the lowest variant price. The list ("before") price
    is only shown when a variant at that lowest price is itself discounted, so
    a full-price size never appears struck through from another size's
    compare-at price.
    """
    priced = []
    for variant in variants:
        price = price_of(variant.get("price"))
        if price is not None:
            priced.append((price, price_of(variant.get("compare_at_price"))))
    if not priced:
        return None, None
    current = min(price for price, _ in priced)
    discounts = [compare for price, compare in priced if price == current and compare is not None and compare > current]
    return current, (max(discounts) if discounts else None)


def option_value(product: dict[str, Any], variant: dict[str, Any], names: Iterable[str]) -> Optional[str]:
    """The variant's value for the first option whose name matches ``names``."""
    wanted = {name.casefold() for name in names}
    for index, option in enumerate(product.get("options") or []):
        name = option.get("name") if isinstance(option, dict) else option
        if str(name or "").casefold() not in wanted:
            continue
        position = (option.get("position") if isinstance(option, dict) else None) or index + 1
        value = variant.get(f"option{position}")
        if value is None and isinstance(variant.get("options"), list) and len(variant["options"]) >= position:
            value = variant["options"][position - 1]
        return text(value)
    return None


def product_type(product: dict[str, Any]) -> Optional[str]:
    return text(product.get("type") or product.get("product_type"))


def images(product: dict[str, Any], base_url: str) -> list[str]:
    found = []
    for value in product.get("images") or []:
        source = value.get("src") if isinstance(value, dict) else value
        if source:
            found.append(urljoin(base_url + "/", str(source)) if str(source).startswith("/") and not str(source).startswith("//")
                         else ("https:" + str(source) if str(source).startswith("//") else str(source)))
    return dedupe(found)


def plain_text(value: Any) -> Optional[str]:
    if not value:
        return None
    raw = BeautifulSoup(str(value), "html.parser").get_text("\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    return "\n\n".join(dedupe(line for line in lines if line)) or None


def text(value: Any) -> Optional[str]:
    cleaned = re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""
    return cleaned or None


def strings(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [part for part in values.split(",")]
    if not isinstance(values, (list, tuple)):
        return []
    return dedupe(cleaned for value in values if (cleaned := text(value)))


def dedupe(values: Iterable[Any]) -> list[Any]:
    seen: set[Any] = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


COLOR_GROUPS = (
    ("Multicolor", ("multi", "mix", "flerfarvet")),
    ("Black", ("black", "sort", "noir", "onyx")),
    ("White", ("white", "hvid", "off white", "off-white", "cream", "creme", "ecru", "ivory", "sail")),
    ("Grey", ("grey", "gray", "grå", "graa", "platinum", "silver", "charcoal", "melange")),
    ("Blue", ("blue", "blå", "blaa", "navy", "indigo", "denim", "royal")),
    ("Green", ("green", "grøn", "groen", "olive", "army", "khaki")),
    ("Brown", ("brown", "brun", "chocolate", "mocha", "tan", "camel", "sand")),
    ("Beige", ("beige", "stone", "taupe", "oat", "bone")),
    ("Red", ("red", "rød", "roed", "burgundy", "bordeaux", "wine")),
    ("Pink", ("pink", "rosa", "lyserød")),
    ("Purple", ("purple", "lilla", "violet")),
    ("Yellow", ("yellow", "gul", "mustard", "lemon")),
    ("Orange", ("orange",)),
)


def color_group(color: Optional[str]) -> Optional[str]:
    """One shared English colour family for HIGHLIDE filters."""
    if not color:
        return None
    normalized = color.casefold()
    for group, words in COLOR_GROUPS:
        if any(re.search(rf"(?<![a-zæøå]){re.escape(word)}(?![a-zæøå])", normalized) for word in words):
            return group
    return None
