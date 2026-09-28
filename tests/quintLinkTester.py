"""Collect product URLs from qUINT clothing categories and run the product tester.

Keep this file in the same directory as quintProductTester.py.
"""

import argparse
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

import quintProductTester


CATEGORY_ROOT = "https://www.quint.dk/toej"
SITE_ROOT = "https://www.quint.dk"
DEFAULT_PAGE_LIMIT = 2
DEFAULT_PRODUCT_LIMIT = 100


async def handle_cookie_prompt(page):
    """Decline optional cookies if the banner appears."""
    decline_button = page.locator("#declineButton")

    try:
        await decline_button.wait_for(state="visible", timeout=3_000)
        await decline_button.click()
        await decline_button.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeoutError:
        # The banner is absent or has already been handled.
        pass


async def find_major_categories(page):
    """Read direct subcategory links from the /toej page."""
    await page.goto(CATEGORY_ROOT, wait_until="domcontentloaded")
    await handle_cookie_prompt(page)
    # Quint's /toej page currently contains two nested <main> elements.
    # Waiting on the unqualified locator would violate Playwright strict mode.
    await page.locator("main").first.wait_for(state="visible", timeout=15_000)

    category_anchors = page.locator('main a[href*="/toej/"]')
    categories = []
    seen_urls = set()

    for index in range(await category_anchors.count()):
        anchor = category_anchors.nth(index)
        href = await anchor.get_attribute("href")
        if not href:
            continue

        category_url = urljoin(SITE_ROOT, href)
        parsed_url = urlparse(category_url)
        path_parts = [part for part in parsed_url.path.strip("/").split("/") if part]

        # /toej/bukser has two path parts. This excludes deeper subcategories.
        if parsed_url.netloc != urlparse(SITE_ROOT).netloc:
            continue
        if len(path_parts) != 2 or path_parts[0].casefold() != "toej":
            continue
        if category_url in seen_urls:
            continue

        label = " ".join((await anchor.inner_text()).split())
        if not label:
            label = (await anchor.get_attribute("title") or "").strip()
        if not label:
            label = path_parts[-1].replace("-", " ").title()

        seen_urls.add(category_url)
        categories.append({"category": label, "url": category_url})

    if not categories:
        raise RuntimeError(
            "No direct /toej subcategory links were found. "
            "The category-link selector may need updating."
        )

    return categories


async def read_product_links_on_page(page, category):
    """Return product links currently shown in a category listing."""
    anchors = page.locator('main a[href*="/produkt/"]')
    products = []
    seen_paths = set()

    for index in range(await anchors.count()):
        anchor = anchors.nth(index)
        href = await anchor.get_attribute("href")
        if not href:
            continue

        product_url = urljoin(SITE_ROOT, href)
        parsed_url = urlparse(product_url)
        if parsed_url.netloc != urlparse(SITE_ROOT).netloc:
            continue

        # One product may appear more than once in the listing. Its path is a
        # stable deduplication key, while the full URL is kept for the tester.
        product_key = parsed_url.path.rstrip("/").casefold()
        if product_key in seen_paths:
            continue
        seen_paths.add(product_key)

        # Product-card image alt text is a useful short name for error messages.
        image = anchor.locator("img[alt]").first
        name = ""
        if await image.count():
            name = (await image.get_attribute("alt") or "").strip()
        if not name:
            name = product_key.rsplit("/", 1)[-1]

        products.append(
            {
                "name": name,
                "url": product_url,
                # This is the category page the crawler found the link on.
                "category": category,
            }
        )

    return products


async def collect_category_products(page, category_info, page_limit):
    """Collect product links from at most page_limit listing pages."""
    category_url = category_info["url"]
    category_name = category_info["category"]

    await page.goto(category_url, wait_until="domcontentloaded")
    await handle_cookie_prompt(page)
    # Some category pages also render more than one <main> element.
    await page.locator("main").first.wait_for(state="visible", timeout=15_000)

    products = []
    seen_paths = set()

    for page_number in range(page_limit):
        product_anchors = page.locator('main a[href*="/produkt/"]')
        try:
            await product_anchors.first.wait_for(state="attached", timeout=10_000)
        except PlaywrightTimeoutError:
            # An empty category page is valid; move on to the next category.
            break

        page_products = await read_product_links_on_page(page, category_name)
        for product in page_products:
            key = urlparse(product["url"]).path.rstrip("/").casefold()
            if key not in seen_paths:
                seen_paths.add(key)
                products.append(product)

        if page_number + 1 >= page_limit:
            break

        # Keep a fingerprint so we can tell when the next page's products load.
        old_hrefs = await product_anchors.evaluate_all(
            "elements => elements.map(element => element.getAttribute('href'))"
        )
        old_fingerprint = "|".join(old_hrefs)

        next_link = page.get_by_role(
            "link", name=re.compile(r"^\s*NÆSTE\s*$", re.IGNORECASE)
        ).last
        if not await next_link.count() or not await next_link.is_visible():
            break

        await next_link.click()
        try:
            await page.wait_for_function(
                """previous => {
                    const current = Array.from(
                        document.querySelectorAll('main a[href*="/produkt/"]')
                    ).map(element => element.getAttribute('href')).join('|');
                    return current.length > 0 && current !== previous;
                }""",
                arg=old_fingerprint,
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            # The next control may be disabled or the site may have changed its
            # pagination behavior. Keep the links collected so far.
            break

    return products


def choose_balanced_sample(products_by_category, limit):
    """Choose up to limit products, spreading the sample across categories."""
    category_names = list(products_by_category)
    selected = []
    positions = {name: 0 for name in category_names}
    seen_product_paths = set()

    while len(selected) < limit:
        advanced_this_round = False
        for category_name in category_names:
            position = positions[category_name]
            bucket = products_by_category[category_name]
            if position >= len(bucket):
                continue

            positions[category_name] += 1
            advanced_this_round = True

            product = bucket[position]
            product_path = urlparse(product["url"]).path.rstrip("/").casefold()
            if product_path in seen_product_paths:
                continue

            seen_product_paths.add(product_path)
            selected.append(product)

            if len(selected) >= limit:
                break

        if not advanced_this_round:
            break

    return selected


async def run_product_tester(page, products):
    """Check each product and append one flat output record per color."""
    results = []

    for index, product in enumerate(products, start=1):
        print(f"Checking {index}/{len(products)}: {product['name']} ({product['category']})")
        try:
            color_records = await quintProductTester.check_product(page, product)
            if not color_records:
                raise RuntimeError("The product checker returned no color records.")

            for color_record in color_records:
                # The category found by the listing crawler is the most reliable
                # product type for these records.
                color_record["product_type"] = product["category"]
            results.extend(color_records)
            print(json.dumps(color_records, ensure_ascii=False, indent=2))
        except Exception as exc:
            error_record = {
                "product_name": product["name"],
                "url": product["url"],
                "product_type": product["category"],
                "error": str(exc),
            }
            results.append(error_record)
            print(json.dumps(error_record, ensure_ascii=False, indent=2))

        if index < len(products):
            await page.wait_for_timeout(1_000)

    return results


async def main():
    parser = argparse.ArgumentParser(
        description="Collect qUINT clothing product links and test them."
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window while the script runs.",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=DEFAULT_PRODUCT_LIMIT,
        help="Maximum number of products sent to quintProductTester.py (default: 100).",
    )
    parser.add_argument(
        "--pages-per-category",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help="Maximum category-listing pages to visit per category (default: 2).",
    )
    args = parser.parse_args()

    if args.max_products < 1 or args.pages_per_category < 1:
        parser.error("--max-products and --pages-per-category must both be at least 1")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        page = await browser.new_page()
        page.set_default_timeout(10_000)

        try:
            categories = await find_major_categories(page)
            print(f"Found {len(categories)} major clothing categories.")

            products_by_category = {}
            for category_info in categories:
                category_name = category_info["category"]
                print(f"Collecting {category_name}: {category_info['url']}")
                products_by_category[category_name] = await collect_category_products(
                    page,
                    category_info,
                    args.pages_per_category,
                )
                print(
                    f"  Found {len(products_by_category[category_name])} unique product link(s)."
                )

            products_to_test = choose_balanced_sample(
                products_by_category,
                args.max_products,
            )

            links_file = Path(__file__).with_name("quint-product-links.json")
            links_file.write_text(
                json.dumps(products_to_test, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Saved {len(products_to_test)} product link(s) to {links_file}")

            results = await run_product_tester(page, products_to_test)

            error_urls = {
                row["url"] for row in results if "error" in row and row.get("url")
            }
            successful_product_urls = {
                product["url"] for product in products_to_test
                if product["url"] not in error_urls
            }
            product_urls_with_color_data = {
                row["url"] for row in results if "error" not in row and row.get("url")
            }
            products_with_stock_urls = {
                row["url"]
                for row in results
                if row.get("stock") in {"in_stock", "few_in_stock"} and row.get("url")
            }
            successful_products_with_color_data = (
                successful_product_urls & product_urls_with_color_data
            )
            products_without_stock_urls = (
                successful_products_with_color_data - products_with_stock_urls
            )
            summary = {
                "products_tested": len(products_to_test),
                "products_checked_successfully": len(successful_product_urls),
                "products_with_stock_at_store": len(products_with_stock_urls),
                "products_without_stock_at_store": len(products_without_stock_urls),
                "products_without_color_data": len(
                    successful_product_urls - product_urls_with_color_data
                ),
                "products_with_errors": len(error_urls),
                "stock_definition": (
                    "A product counts once if any color has at least one size in_stock or few_in_stock."
                ),
            }

            print("\nStock summary:")
            print(json.dumps(summary, ensure_ascii=False, indent=2))

            results_file = Path(__file__).with_name("quintLinkTester-results.json")
            results_file.write_text(
                json.dumps(results, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Saved {len(results)} color/error record(s) to {results_file}")

            summary_file = Path(__file__).with_name("quintLinkTester-summary.json")
            summary_file.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"Saved the stock summary to {summary_file}")
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
