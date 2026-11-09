"""Shared completeness checks for local-inventory parsers.

Every store parser follows one contract: when a source answers but leaves out
data we need to describe stock, raise ``SourcePayloadError``. The daily
refresh then counts the row as failed and keeps the saved observation, instead
of writing zero stock or "unavailable" for data it never saw.
"""
from __future__ import annotations

from typing import Any, Callable, Hashable, Iterable, Mapping

from scrapers.source_http import SourcePayloadError


def require_present(
    records: Mapping[Hashable, Any],
    expected: Iterable[Hashable],
    what: str,
) -> None:
    """Raise unless every expected key has a record."""
    missing = [key for key in expected if key not in records]
    if missing:
        raise SourcePayloadError(f"{what} omitted {len(missing)} expected item(s): {missing[:10]!r}")


def require_nonempty(value: Any, what: str) -> None:
    if not value:
        raise SourcePayloadError(f"{what} was empty; retaining previous stock")


def unique_sizes(
    variants: Iterable[dict[str, Any]],
    size_of: Callable[[dict[str, Any]], str],
    what: str,
) -> dict[str, str]:
    """Map variant id -> size label, raising on missing or duplicate labels.

    Two variants sharing a label would overwrite each other's stock in the
    ``sizes`` object while totals still counted both.
    """
    sizes: dict[str, str] = {}
    for variant in variants:
        if variant.get("id") is None:
            continue
        size = str(size_of(variant) or "").strip()
        if not size:
            raise SourcePayloadError(f"{what} variant {variant['id']} has no size label")
        sizes[str(variant["id"])] = size
    labels = list(sizes.values())
    if len(set(labels)) != len(labels):
        duplicates = sorted({label for label in labels if labels.count(label) > 1})
        raise SourcePayloadError(f"{what} has duplicate size labels: {duplicates!r}")
    return sizes
