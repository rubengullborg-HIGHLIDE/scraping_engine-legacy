#!/usr/bin/env python3
"""Discover men's clothing categories and products, then check product stock."""
from __future__ import annotations
from urllib.parse import urlparse, urldefrag, urljoin
import argparse
import json
import re
from collections import Counter
from pathlib import Path


from playwright.sync_api import sync_playwright

from magasinProductTester import scrape_product

COOKIE = "#declineButton"
PRODUCT_PATH = re.compile(r"^/[^?#]+/(?:S\d{6,}|[A-Z0-9-]{6,})\.html$", re.I)


def _accept_cookie(page) -> None:
    button = page.locator(COOKIE)

    try:
        # The banner can appear after the main content, so wait briefly for it.
        button.wait_for(state="visible", timeout=2500)
    except Exception:
        return

    button.first.click(timeout=5000)
    button.first.wait_for(state="hidden", timeout=7000)


def _discover_major_categories(page, start_url: str) -> list[dict]:
    """Read the labeled category links from the men's clothing landing page."""
    page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
    page.locator("main").wait_for(state="visible", timeout=20000)
    _accept_cookie(page)

    nav = page.locator('ul[is="adaptive-category-nav"]')
    nav.wait_for(state="visible", timeout=15000)

    items = nav.locator('a.b-category-navigation__link[href]').evaluate_all(
        """els => els.map(a => ({
            url: a.href,
            name: (
                a.querySelector('.b-category-navigation__title')?.innerText
                || a.innerText
                || ''
            ).trim()
        }))"""
    )

    categories = []
    seen_urls = set()

    for item in items:
        url = urldefrag(item["url"])[0]
        parsed = urlparse(url)
        name = re.sub(r"\s+", " ", item["name"]).strip()

        if (
            parsed.netloc.endswith("magasin.dk")
            and parsed.path.startswith("/herre/toej/")
            and parsed.path.endswith("/")
            and name
            and url not in seen_urls
        ):
            categories.append({"name": name, "url": url})
            seen_urls.add(url)

    return categories


def discover_category_links(
    page,
    start_url: str,
    max_pages_per_category: int,
    max_products: int,
    max_categories: int = 0,
) -> tuple[list[dict], list[dict]]:
    """Discover categories, then collect and deduplicate their product links."""
    from urllib.parse import urljoin

    def dismiss_cookie_banner() -> None:
        button = page.locator(COOKIE)

        try:
            # The banner can appear shortly after the page's main content.
            button.wait_for(state="visible", timeout=2500)
        except Exception:
            return

        try:
            button.first.click(timeout=5000)
            button.first.wait_for(state="hidden", timeout=7000)
        except Exception as exc:
            print(
                f"[discover] cookie banner did not close cleanly: {exc}",
                flush=True,
            )

    categories = _discover_major_categories(page, start_url)
    # Check again in case the banner appeared after category discovery began.
    dismiss_cookie_banner()

    if max_categories:
        categories = categories[:max_categories]

    products: dict[str, dict] = {}

    for category_index, category in enumerate(categories, 1):
        if len(products) >= max_products:
            break

        print(
            f"[discover] category {category_index}/{len(categories)}: "
            f"{category['name']} — {category['url']}",
            flush=True,
        )

        page.goto(
            category["url"],
            wait_until="domcontentloaded",
            timeout=45000,
        )
        page.locator("main").wait_for(
            state="visible",
            timeout=20000,
        )
        dismiss_cookie_banner()

        for page_number in range(1, max_pages_per_category + 1):
            # Product URLs match the detail-page pattern, which filters out
            # advertising links mixed between product cards.
            found = page.locator(
                'main a[href*=".html"]'
            ).evaluate_all(
                """els => els.map(a => ({
                    href: a.href,
                    label: (
                        a.getAttribute('aria-label')
                        || a.innerText
                        || ''
                    ).trim()
                }))"""
            )

            previous_product_count = len(products)

            for item in found:
                href = urldefrag(item["href"])[0]
                parsed = urlparse(href)

                if (
                    not parsed.netloc.endswith("magasin.dk")
                    or not PRODUCT_PATH.match(parsed.path)
                ):
                    continue

                product = products.setdefault(
                    href,
                    {
                        "url": href,
                        "name": re.sub(
                            r"\s+",
                            " ",
                            item["label"].replace("Se produkt", ""),
                        ).strip(),
                        "categories": [],
                    },
                )

                if not any(
                    existing["url"] == category["url"]
                    for existing in product["categories"]
                ):
                    product["categories"].append(category.copy())

                if len(products) >= max_products:
                    break

            newly_found = len(products) - previous_product_count
            print(
                f"[discover]   listing page "
                f"{page_number}/{max_pages_per_category}: "
                f"{newly_found} new, {len(products)} total",
                flush=True,
            )

            if (
                len(products) >= max_products
                or page_number == max_pages_per_category
            ):
                break

            more = page.get_by_role(
                "link",
                name=re.compile(r"Vis \d+ produkter mere", re.I),
            )

            if not more.count() or not more.first.is_visible():
                break

            # The link in your traceback has an href such as ?page=2.
            # Navigate to that page directly so an overlay cannot block a click.
            next_href = more.first.get_attribute("href")

            if next_href:
                next_url = urljoin(page.url, next_href)
                parsed_next = urlparse(next_url)

                if (
                    not parsed_next.netloc.endswith("magasin.dk")
                    or not parsed_next.path.startswith("/herre/toej/")
                ):
                    print(
                        "[discover]   stopping pagination because the next "
                        f"URL is outside men's clothing: {next_url}",
                        flush=True,
                    )
                    break

                if next_url == page.url:
                    print(
                        "[discover]   stopping pagination because the next "
                        "URL matches the current page",
                        flush=True,
                    )
                    break

                try:
                    page.goto(
                        next_url,
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    page.locator("main").wait_for(
                        state="visible",
                        timeout=20000,
                    )
                    dismiss_cookie_banner()
                except Exception as exc:
                    print(
                        f"[discover]   pagination navigation failed: {exc}",
                        flush=True,
                    )
                    break

            else:
                # Fallback if Magasin provides a load-more link without an href.
                dismiss_cookie_banner()
                previous_link_count = page.locator(
                    'main a[href*=".html"]'
                ).count()

                try:
                    more.first.click(timeout=7000)
                    page.wait_for_function(
                        """count => document.querySelectorAll(
                            'main a[href*=".html"]'
                        ).length > count""",
                        arg=previous_link_count,
                        timeout=12000,
                    )
                except Exception as exc:
                    print(
                        f"[discover]   load-more failed: {exc}",
                        flush=True,
                    )
                    break

    return list(products.values())[:max_products], categories


def _make_summary(
    links: list[dict],
    successes: list[dict],
    errors: list[dict],
    tested_urls: set[str],
) -> dict:
    """Count crawl outcomes once per product URL."""
    records_by_url = {
        item["product_url"]: item["records"]
        for item in successes
    }
    errors_by_url = {
        item["product_url"]: item["errors"]
        for item in errors
    }
    product_urls = {item["url"] for item in links}

    with_stock = 0
    without_stock = 0
    unknown_stock = 0
    without_color = 0
    with_errors = 0
    checked_successfully = 0
    per_product: dict[str, str] = {}

    for url in product_urls:
        if url not in tested_urls:
            continue

        records = records_by_url.get(url, [])
        product_errors = errors_by_url.get(url, [])

        if product_errors:
            with_errors += 1

        if records:
            checked_successfully += 1
        else:
            without_color += 1

        has_stock = any(
            record.get("sizes_in_stock")
            or record.get("sizes_few_in_stock")
            for record in records
        )

        if has_stock:
            with_stock += 1
            per_product[url] = "in_stock"
        elif not records or product_errors:
            # Errors mean stock was not confirmed, so do not call it out of stock.
            unknown_stock += 1
            per_product[url] = "unknown"
        elif all(
            record.get("stock") in {"out_of_stock", "sold_out_all_stores"}
            for record in records
        ):
            without_stock += 1
            per_product[url] = "without_stock"
        else:
            unknown_stock += 1
            per_product[url] = "unknown"

    tested_count = len(tested_urls)

    return {
        "products_tested": tested_count,
        "products_checked_successfully": checked_successfully,
        "products_with_stock_at_store": with_stock,
        "products_without_stock_at_store": without_stock,
        "products_stock_status_unknown": unknown_stock,
        "products_without_color_data": without_color,
        "products_with_errors": with_errors,
        "stock_definition": (
            "A product counts once if any color has at least one size "
            "in_stock or few_in_stock."
        ),
        "no_stock_definition": (
            "Counted only when all returned color records explicitly report "
            "out_of_stock or sold_out_all_stores and the product has no tester errors."
        ),
        "stock_summary": dict(Counter(per_product.values())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start-url",
        default="https://www.magasin.dk/herre/toej/",
    )
    parser.add_argument(
        "--max-pages-per-category",
        "--pages-per-category",
        "--pages_per_category",
        dest="max_pages_per_category",
        type=int,
        default=1,
        help="Maximum listing pages to read within each category.",
    )
    parser.add_argument(
        "--max-categories",
        type=int,
        default=0,
        help="Maximum categories to visit; 0 means all categories found.",
    )
    parser.add_argument("--max-products", type=int, default=3)
    parser.add_argument("--store", default="Magasin Aarhus")
    parser.add_argument(
        "--links-output",
        default="magasin_product_links.json",
    )
    parser.add_argument(
        "--results-output",
        default="magasin_crawl_results.json",
    )
    parser.add_argument(
        "--show-browser",
        "--headed",
        dest="show_browser",
        action="store_true",
    )

    args = parser.parse_args()

    if (
        args.max_pages_per_category < 1
        or args.max_products < 1
        or args.max_categories < 0
    ):
        parser.error(
            "pages-per-category and max-products must be >= 1; "
            "max-categories must be >= 0"
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not args.show_browser
        )
        page = browser.new_page(locale="da-DK")

        try:
            # Reuse the same tab for category discovery and product checks.
            links, categories = discover_category_links(
                page,
                args.start_url,
                args.max_pages_per_category,
                args.max_products,
                args.max_categories,
            )

            print(
                f"[discover] found {len(links)} unique product(s); "
                "checking stock",
                flush=True,
            )

            Path(args.links_output).write_text(
                json.dumps(
                    {
                        "categories": categories,
                        "products": links,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            successes = []
            errors = []
            tested_urls = set()

            for index, product in enumerate(links, 1):
                label = product["name"] or product["url"]
                print(
                    f"[product {index}/{len(links)}] {label}",
                    flush=True,
                )

                try:
                    result = scrape_product(
                        product["url"],
                        product["name"],
                        args.store,
                        headless=not args.show_browser,
                        page=page,
                    )
                except Exception as exc:
                    result = {
                        "records": [],
                        "errors": [
                            {
                                "stage": "product_tester_call",
                                "error": str(exc),
                            }
                        ],
                    }

                tested_urls.add(product["url"])

                if result["records"]:
                    successes.append(
                        {
                            "product_url": product["url"],
                            "product_name": product["name"],
                            "categories": product["categories"],
                            "records": [
                                {
                                    **record,
                                    "categories": product["categories"],
                                }
                                for record in result["records"]
                            ],
                        }
                    )

                if result["errors"]:
                    errors.append(
                        {
                            "product_url": product["url"],
                            "product_name": product["name"],
                            "categories": product["categories"],
                            "errors": result["errors"],
                        }
                    )

                if not result["records"] and not result["errors"]:
                    errors.append(
                        {
                            "product_url": product["url"],
                            "product_name": product["name"],
                            "categories": product["categories"],
                            "errors": [
                                {
                                    "stage": "product_tester",
                                    "error": "No records returned",
                                }
                            ],
                        }
                    )

                print(
                    f"[product {index}/{len(links)}] "
                    f"records={len(result['records'])} "
                    f"errors={len(result['errors'])}",
                    flush=True,
                )

                # Write progress after each product so interrupted runs keep results.
                summary = _make_summary(
                    links,
                    successes,
                    errors,
                    tested_urls,
                )
                output = {
                    "summary": summary,
                    "products": successes,
                    "errors": errors,
                    "stock_summary": summary["stock_summary"],
                }

                Path(args.results_output).write_text(
                    json.dumps(output, ensure_ascii=False, indent=2)
                    + "\n",
                    encoding="utf-8",
                )

            print(
                f"Discovered {len(links)} products across "
                f"{len(categories)} categories; wrote "
                f"{args.links_output} and {args.results_output}"
            )

        finally:
            browser.close()


if __name__ == "__main__":
    main()
