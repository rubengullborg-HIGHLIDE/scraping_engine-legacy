"""Crawl Salling men's clothing and shoe categories and test products."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.sync_api import sync_playwright

from sallingProductTester import (
    dismiss_cookie_banner,
    is_dame_url,
    test_product_on_page,
)


DEFAULT_CLOTHING_URL = "https://salling.dk/herre/toej/c-11905/"
DEFAULT_SHOES_URL = "https://salling.dk/herre/sko/c-1002/"


def normalized_url(url: str) -> str:
    """Remove query strings and trailing slashes for URL comparisons."""
    parts = urlsplit(url)
    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path.rstrip("/"),
        "",
        "",
    ))


def clean_text(value: str | None) -> str:
    """Collapse whitespace and return a clean string."""
    return re.sub(r"\s+", " ", value or "").strip()


def discover_categories_from_root(
    page,
    root_url: str,
    department: str,
) -> list[dict]:
    """Get subcategory links from a department landing page."""
    page.goto(root_url, wait_until="domcontentloaded", timeout=60000)
    page.locator("h1").first.wait_for(state="visible", timeout=20000)
    dismiss_cookie_banner(page)

    try:
        page.locator(
            "ul.category-swiper__list a.category-swiper__button"
        ).first.wait_for(state="visible", timeout=6000)
    except Exception:
        pass

    root_name = ""
    try:
        root_name = clean_text(page.locator("h1").first.inner_text())
    except Exception:
        pass

    categories = page.locator(
        "ul.category-swiper__list a.category-swiper__button"
    ).evaluate_all(
        """(links, baseUrl) => links.map(link => ({
            url: new URL(link.getAttribute('href'), baseUrl).href,
            name: (link.innerText || '').trim().replace(/\\s+/g, ' ')
        }))""",
        root_url,
    )

    result = []
    seen = set()

    for category in categories:
        url = category.get("url", "")
        name = clean_text(category.get("name", ""))
        key = normalized_url(url)

        if url and name and key not in seen:
            seen.add(key)
            result.append({
                "url": url,
                "name": name,
                "department": department,
            })

    if not result:
        result.append({
            "url": root_url,
            "name": root_name or department,
            "department": department,
        })

    return result


def discover_products_on_page(
    page,
    category_url: str,
    category_name: str,
    department: str,
) -> list[dict]:
    """Collect deduplicated product links from one category page."""
    products = page.locator("a[href*='/p-']").evaluate_all(
        """(links, baseUrl) => {
            const seen = new Set();

            return links
                .map(link => {
                    const href = link.getAttribute('href');
                    const card = link.closest(
                        '[class*="product-card"], ' +
                        '[class*="product-tile"], article'
                    );
                    const title = card?.querySelector(
                        '[class*="product-card__title"], ' +
                        '[class*="product-tile__title"], h2, h3'
                    );

                    const name = (
                        title?.innerText ||
                        link.getAttribute('aria-label') ||
                        link.getAttribute('title') ||
                        ''
                    ).trim().replace(/\\s+/g, ' ');

                    return {
                        url: href ? new URL(href, baseUrl).href : '',
                        product_name: name
                    };
                })
                .filter(item => {
                    if (!item.url.includes('/p-') || seen.has(item.url)) {
                        return false;
                    }
                    seen.add(item.url);
                    return true;
                });
        }""",
        category_url,
    )

    # Exclude Dame links before returning the discovered product list.
    products = [
        product for product in products
        if not is_dame_url(product.get("url", ""))
    ]

    for product in products:
        product["category_name"] = category_name
        product["category_url"] = category_url
        product["department"] = department

    return products


def next_page_url(page) -> str | None:
    """Return the next pagination URL, or None on the final page."""
    pagination = page.locator(
        ".category-list__pagination .base-pagination"
    )

    if not pagination.count():
        return None

    next_link = pagination.get_by_role(
        "link",
        name=re.compile(r"^\s*Næste\s*$", re.I),
    )
    if next_link.count():
        href = next_link.first.get_attribute("href")
        if href:
            return urljoin(page.url, href)

    next_button = pagination.get_by_role(
        "button",
        name=re.compile(r"^\s*Næste\s*$", re.I),
    )
    if next_button.count() and next_button.first.is_disabled():
        return None

    return None


def collect_product_links(
    page,
    categories: list[dict],
    max_pages_per_category: int,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Collect product links by category up to the configured page limit."""
    products_by_category = {}
    errors = []

    for category in categories:
        category_key = normalized_url(category["url"])
        products_by_category[category_key] = []
        seen_products_in_category = set()

        current_url = category["url"]
        seen_pages = set()

        for _ in range(max_pages_per_category):
            page_key = normalized_url(current_url)
            if page_key in seen_pages:
                break
            seen_pages.add(page_key)

            try:
                page.goto(
                    current_url,
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                page.locator("h1").first.wait_for(
                    state="visible",
                    timeout=20000,
                )
                dismiss_cookie_banner(page)

                products = discover_products_on_page(
                    page,
                    current_url,
                    category["name"],
                    category.get("department", ""),
                )

                for product in products:
                    product_key = normalized_url(product["url"])
                    if product_key not in seen_products_in_category:
                        seen_products_in_category.add(product_key)
                        products_by_category[category_key].append(product)

                current_url = next_page_url(page)
                if not current_url:
                    break

            except Exception as exc:
                errors.append({
                    "department": category.get("department", ""),
                    "category_name": category["name"],
                    "category_url": category["url"],
                    "page_url": current_url,
                    "error": str(exc),
                    "stage": "category_crawl",
                })
                break

    return products_by_category, errors


def stock_priority(status: str) -> int:
    """Return the priority used when combining color records."""
    return {
        "in_stock": 4,
        "few_in_stock": 3,
        "unknown": 2,
        "out_of_stock": 1,
    }.get(status, 2)


def build_run_summary(
    attempted_products: list[dict],
    records: list[dict],
    product_errors: list[dict],
) -> dict:
    """Summarize products once, rather than once per color record."""
    records_by_product = {}
    errors_by_product = {}

    for record in records:
        source_url = record.get("source_product_url") or record.get("url", "")
        key = normalized_url(source_url)
        if key:
            records_by_product.setdefault(key, []).append(record)

    for error in product_errors:
        source_url = error.get("source_product_url") or error.get("url", "")
        key = normalized_url(source_url)
        if key:
            errors_by_product.setdefault(key, []).append(error)

    products_checked_successfully = 0
    products_with_errors = 0
    products_with_stock = 0
    products_without_stock = 0
    products_without_color_data = 0
    products_with_unknown_stock = 0
    seen_products = set()

    for product in attempted_products:
        key = normalized_url(product.get("url", ""))

        if not key or key in seen_products:
            continue
        seen_products.add(key)

        product_records = records_by_product.get(key, [])
        product_errors_for_url = errors_by_product.get(key, [])

        if not product_records or product_errors_for_url:
            products_with_errors += 1
            continue

        products_checked_successfully += 1

        if not any(record.get("color") for record in product_records):
            products_without_color_data += 1

        statuses = {
            record.get("stock", "unknown")
            for record in product_records
        }

        has_positive_stock = any(
            record.get("stock") in ("in_stock", "few_in_stock")
            or record.get("sizes_in_stock")
            or record.get("sizes_few_in_stock")
            for record in product_records
        )

        if has_positive_stock:
            products_with_stock += 1
        else:
            products_without_stock += 1
            if "unknown" in statuses or not statuses:
                products_with_unknown_stock += 1

    return {
        "products_tested": len(seen_products),
        "products_checked_successfully": products_checked_successfully,
        "products_with_stock_at_store": products_with_stock,
        "products_without_stock_at_store": products_without_stock,
        "products_without_color_data": products_without_color_data,
        "products_with_errors": products_with_errors,
        "products_with_unknown_stock": products_with_unknown_stock,
        "stock_definition": (
            "A product counts once if any color has at least one size "
            "in_stock or few_in_stock. products_without_stock_at_store means "
            "no positive stock signal was found; it includes unknown "
            "availability and does not mean confirmed out of stock."
        ),
    }


def run(
    clothing_url: str = DEFAULT_CLOTHING_URL,
    shoes_url: str | None = DEFAULT_SHOES_URL,
    store: str = "Salling Aarhus",
    max_pages_per_category: int = 1,
    max_products_per_category: int = 2,
    out_dir: str = "tests/output",
    headless: bool = True,
) -> dict:
    """Crawl clothing and shoe categories and test a bounded number."""
    if max_pages_per_category < 1:
        raise ValueError("max_pages_per_category must be at least 1")
    if max_products_per_category < 1:
        raise ValueError("max_products_per_category must be at least 1")

    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    crawl_errors = []
    product_errors = []
    records = []
    attempted_products = []
    covered_sku_urls = set()
    categories = []
    products_by_category = {}
    category_test_counts = {}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="da-DK")
        page = context.new_page()

        try:
            categories.extend(
                discover_categories_from_root(
                    page,
                    clothing_url,
                    "mens_clothing",
                )
            )

            if shoes_url:
                categories.extend(
                    discover_categories_from_root(
                        page,
                        shoes_url,
                        "mens_shoes",
                    )
                )

            products_by_category, crawl_errors = collect_product_links(
                page,
                categories,
                max_pages_per_category,
            )

            all_product_links = []
            for category in categories:
                category_key = normalized_url(category["url"])
                all_product_links.extend(
                    products_by_category.get(category_key, [])
                )

            (output_dir / "product_links.json").write_text(
                json.dumps(
                    {
                        "clothing_start_url": clothing_url,
                        "shoes_start_url": shoes_url,
                        "categories": categories,
                        "discovered_product_link_count": len(all_product_links),
                        "products_by_category": products_by_category,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            category_test_counts = {
                normalized_url(category["url"]): 0
                for category in categories
            }

            # Reuse one browser page/context for all product tests.
            for category in categories:
                category_key = normalized_url(category["url"])
                candidates = products_by_category.get(category_key, [])

                for product in candidates:
                    if (
                        category_test_counts[category_key]
                        >= max_products_per_category
                    ):
                        break

                    product_url = product["url"]

                    # Defensive filtering if a product list was edited or
                    # loaded from somewhere other than discovery.
                    if is_dame_url(product_url):
                        continue

                    product_key = normalized_url(product_url)

                    # Skip links already covered as a color or size variant
                    # by an earlier product test.
                    if product_key in covered_sku_urls:
                        continue

                    category_test_counts[category_key] += 1
                    attempted_products.append(product)

                    try:
                        result = test_product_on_page(
                            page,
                            product_url,
                            product.get("product_name", ""),
                            store,
                        )

                        covered_sku_urls.add(product_key)
                        for sku_url in result.get("tested_size_urls", []):
                            covered_sku_urls.add(normalized_url(sku_url))

                        product_records = result.get("records", [])

                        for record in product_records:
                            record.setdefault("source_product_url", product_url)

                            if not record.get("source_product_name"):
                                record["source_product_name"] = (
                                    product.get("product_name", "")
                                    or record.get("product_name", "")
                                )

                            record.setdefault(
                                "source_category_name",
                                category["name"],
                            )
                            record.setdefault(
                                "source_category_url",
                                category["url"],
                            )
                            record.setdefault(
                                "source_department",
                                category.get("department", ""),
                            )

                        records.extend(product_records)

                        for error in result.get("errors", []):
                            error_record = dict(error)
                            error_record.setdefault(
                                "source_product_url",
                                product_url,
                            )
                            error_record.setdefault(
                                "source_product_name",
                                product.get("product_name", ""),
                            )
                            error_record.setdefault(
                                "source_category_name",
                                category["name"],
                            )
                            error_record.setdefault(
                                "source_department",
                                category.get("department", ""),
                            )
                            product_errors.append(error_record)

                        if not product_records:
                            product_errors.append({
                                "url": product_url,
                                "source_product_url": product_url,
                                "product_name": product.get("product_name", ""),
                                "category_name": category["name"],
                                "department": category.get("department", ""),
                                "error": "Product tester returned no records",
                                "stage": "product_test",
                            })

                    except Exception as exc:
                        covered_sku_urls.add(product_key)
                        product_errors.append({
                            "url": product_url,
                            "source_product_url": product_url,
                            "product_name": product.get("product_name", ""),
                            "category_name": category["name"],
                            "department": category.get("department", ""),
                            "error": str(exc),
                            "stage": "product_test",
                        })

        finally:
            context.close()
            browser.close()

    # Combine color records so each source product is counted once.
    family_map = {}

    for record in records:
        source_url = record.get("source_product_url") or record.get("url", "")
        family_key = normalized_url(source_url)

        family = family_map.setdefault(
            family_key,
            {
                "product_url": source_url,
                "product_name": record.get("product_name", ""),
                "category_name": record.get("source_category_name", ""),
                "department": record.get("source_department", ""),
                "stock": "out_of_stock",
                "color_record_count": 0,
            },
        )

        family["color_record_count"] += 1

        status = record.get("stock", "unknown")
        if stock_priority(status) > stock_priority(family["stock"]):
            family["stock"] = status

    families = list(family_map.values())

    category_counts = []
    for category in categories:
        category_key = normalized_url(category["url"])
        category_counts.append({
            "name": category["name"],
            "url": category["url"],
            "department": category.get("department", ""),
            "discovered_product_link_count": len(
                products_by_category.get(category_key, [])
            ),
            "tested_product_count": category_test_counts.get(category_key, 0),
        })

    run_summary = build_run_summary(
        attempted_products,
        records,
        product_errors,
    )

    combined = {
        "clothing_start_url": clothing_url,
        "shoes_start_url": shoes_url,
        "store": store,
        "max_products_per_category": max_products_per_category,
        "max_pages_per_category": max_pages_per_category,
        "category_count": len(categories),
        "categories": categories,
        "category_test_counts": category_counts,
        "discovered_product_link_count": sum(
            len(items) for items in products_by_category.values()
        ),
        "product_count": len(attempted_products),
        "color_record_count": len(records),
        "overall_stock_summary": {
            "products_in_stock": sum(
                item["stock"] == "in_stock" for item in families
            ),
            "products_few_in_stock": sum(
                item["stock"] == "few_in_stock" for item in families
            ),
            "products_out_of_stock": sum(
                item["stock"] == "out_of_stock" for item in families
            ),
            "products_unknown": sum(
                item["stock"] == "unknown" for item in families
            ),
            "products": families,
        },
        "run_summary": run_summary,
        "records": records,
        "crawl_errors": crawl_errors,
        "product_errors": product_errors,
    }

    (output_dir / "product_results.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return combined


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--clothing-url",
        default=DEFAULT_CLOTHING_URL,
        help="Men's clothing landing page URL.",
    )
    parser.add_argument(
        "--shoes-url",
        default=DEFAULT_SHOES_URL,
        help="Men's shoes landing page URL. Use --no-shoes to skip it.",
    )
    parser.add_argument("--no-shoes", action="store_true")
    parser.add_argument("--store", default="Salling Aarhus")
    parser.add_argument("--max-pages-per-category", type=int, default=1)
    parser.add_argument(
        "--max-products-per-category",
        type=int,
        default=2,
    )
    parser.add_argument("--out-dir", default="tests/output")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    result = run(
        clothing_url=args.clothing_url,
        shoes_url=None if args.no_shoes else args.shoes_url,
        store=args.store,
        max_pages_per_category=args.max_pages_per_category,
        max_products_per_category=args.max_products_per_category,
        out_dir=args.out_dir,
        headless=not args.headed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
