"""Helpers shared by the Shopify storefront scrapers.

Start of the shared Shopify layer: new Shopify stores should use these instead
of copying another store's private helpers.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

Price = Optional[float]


def variant_prices(
    variants: Iterable[dict[str, Any]],
    price_of: Callable[[Any], Price],
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
