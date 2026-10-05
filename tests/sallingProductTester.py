"""Check Salling product colors, sizes, images, and store availability."""
from __future__ import annotations

import argparse
import json
import re
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright


BASE_URL = "https://salling.dk"
COOKIE_BUTTON = "#usercentrics-cmp-ui button#deny"


def is_dame_url(url: str) -> bool:
    """Return True when the URL contains the excluded text 'dame'."""
    return "dame" in (url or "").casefold()


def clean(text: str | None) -> str:
    """Collapse whitespace and return a clean string."""
    return re.sub(r"\s+", " ", text or "").strip()


def clean_product_name(value: str) -> str:
    """Keep only the product name before its first comma."""
    return clean(value).split(",", 1)[0].strip()


def close_stock_panel(page) -> None:
    """Close the visible Lagerstatus popup, if it is open."""
    panel = page.locator(".overlay:visible").filter(
        has_text=re.compile(r"Lagerstatus", re.I)
    )

    if not panel.count():
        return

    close_button = panel.get_by_role(
        "button",
        name=re.compile(r"^\s*Luk\s*$", re.I),
    )

    if close_button.count():
        try:
            close_button.first.click(timeout=2000)
            panel.first.wait_for(state="hidden", timeout=3000)
            return
        except Exception:
            pass

    reset_button = panel.locator("button.reset-button")
    if reset_button.count():
        try:
            reset_button.first.click(timeout=2000)
            panel.first.wait_for(state="hidden", timeout=3000)
        except Exception:
            pass


def dismiss_cookie_banner(page) -> bool:
    """Click 'Kun nødvendige' if the Usercentrics consent button is visible."""
    button = page.locator(COOKIE_BUTTON).first

    try:
        if not button.is_visible():
            return False
    except Exception:
        return False

    try:
        button.click(timeout=2500)
    except Exception:
        try:
            button.click(force=True, timeout=1500)
        except Exception:
            return False

    try:
        button.wait_for(state="hidden", timeout=3000)
    except Exception:
        pass

    return True


def open_product(page, url: str) -> None:
    """Navigate in the existing tab and wait for the product heading."""
    if is_dame_url(url):
        raise ValueError(f"Excluded URL contains 'dame': {url}")

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.locator("main h1").first.wait_for(state="visible", timeout=20000)
    dismiss_cookie_banner(page)


def selected_variant_labels(main) -> tuple[str, str]:
    """Return the selected color and size labels shown on the product page."""
    selected_color = ""
    selected_size = ""

    for label in main.locator(".variant-selector__label").all():
        text = clean(label.inner_text())

        color_match = re.match(r"^Farve:\s*(.*)$", text, re.I)
        if color_match:
            selected_color = re.split(
                r"\s+(?:Størrelse|Lagerstatus):",
                color_match.group(1),
                maxsplit=1,
                flags=re.I,
            )[0].strip()

        size_match = re.match(r"^Størrelse:\s*(.*)$", text, re.I)
        if size_match:
            selected_size = size_match.group(1).strip()

    if not selected_size:
        size_locator = main.locator(
            ".dropdown__trigger .variant-container"
        ).first
        if size_locator.count():
            selected_size = clean(size_locator.inner_text())

    return selected_color, selected_size


def remove_variant_suffixes(value: str, color: str, size: str) -> str:
    """Remove matching trailing color and size from a product heading."""
    result = clean(value)

    for suffix in (size, color):
        if suffix:
            result = re.sub(
                r",\s*" + re.escape(suffix) + r"\s*$",
                "",
                result,
                flags=re.I,
            ).strip()

    return result


def parse_dkk_amount(value: str) -> float | None:
    """Parse Danish prices such as '1.299,95 kr.' into a number."""
    match = re.search(r"([\d.]+(?:,\d{1,2})?)\s*kr\.?", value, re.I)
    if not match:
        return None

    amount_text = match.group(1).replace(".", "").replace(",", ".")
    try:
        return float(amount_text)
    except ValueError:
        return None


def product_shared(page, product_name: str, url: str) -> dict:
    """Read product fields shared between colors and sizes."""
    main = page.locator("main").first
    heading = clean(main.locator("h1").first.inner_text())
    selected_color, selected_size = selected_variant_labels(main)

    base_heading = remove_variant_suffixes(
        heading,
        selected_color,
        selected_size,
    )
    supplied_name = remove_variant_suffixes(
        product_name,
        selected_color,
        selected_size,
    )

    if supplied_name and (
        supplied_name.casefold() == base_heading.casefold()
        or base_heading.casefold().startswith(supplied_name.casefold())
    ):
        shared_name = supplied_name
    else:
        shared_name = base_heading or supplied_name or heading

    brand_locator = main.locator("h4 a")
    brand = (
        clean(brand_locator.first.inner_text())
        if brand_locator.count()
        else ""
    )

    # These selectors match the observed price markup. The "Spar" label is
    # deliberately not treated as the current price.
    current_price_locator = main.locator(".price__current-price").first
    previous_price_locator = main.locator(".price__before-price").first

    price = (
        clean(current_price_locator.inner_text())
        if current_price_locator.count()
        else ""
    )
    previous_price = (
        clean(previous_price_locator.inner_text())
        if previous_price_locator.count()
        else ""
    )

    current_amount = parse_dkk_amount(price)
    previous_amount = parse_dkk_amount(previous_price)

    save_label = main.locator(".label").filter(
        has_text=re.compile(r"\bSpar\b", re.I)
    )
    has_save_label = save_label.count() > 0

    on_sale = has_save_label or bool(
        current_amount is not None
        and previous_amount is not None
        and previous_amount > current_amount
    )

    sale_percentage = None
    if (
        current_amount is not None
        and previous_amount is not None
        and previous_amount > current_amount
    ):
        sale_percentage = round(
            (1 - current_amount / previous_amount) * 100
        )

    info_text = " ".join(
        clean(text) for text in main.locator("li").all_text_contents()
    )
    fit_match = re.search(
        r"\b(regular|relaxed|modern|slim|loose|straight|oversized)"
        r"(?:\s+\w+){0,2}\s+fit\b",
        info_text,
        re.I,
    )
    fit = fit_match.group(0).strip() if fit_match else ""

    description_locator = main.locator(
        ".product-page__information-long-description"
    )
    description = ""
    if description_locator.count():
        description = clean(description_locator.first.text_content())

    return {
        "product_name": shared_name or clean_product_name(product_name or heading),
        "product_type": "",
        "brand": brand,
        "fit": fit,
        "price": price,
        "on_sale": on_sale,
        "sale_percentage": sale_percentage,
        "description": description,
        "url": url,
    }


def get_color_variants(page) -> list[tuple[str, str]]:
    """
    Return (product_url, color_name) pairs.

    Shoe colors are read from product links in the selector group associated
    with the Farve label. Clothing pages can use #product-variants instead.
    """
    variants = []
    seen_urls = set()

    # The shoe color option contains an image and text in a
    # div.d-flex.align-items-center. Find selector groups near a Farve label,
    # then read the href and the color text from each option.
    color_options = page.locator(".variant-selector__items").evaluate_all(
        """groups => {
            const clean = value => (value || '').replace(/\\s+/g, ' ').trim();
            const results = [];

            for (const group of groups) {
                let node = group.parentElement;
                let isColorGroup = false;

                // Check nearby parents for a label identifying this as the
                // color selector. This avoids treating size links as colors.
                for (
                    let depth = 0;
                    node && depth < 5;
                    depth++, node = node.parentElement
                ) {
                    const labels = [
                        ...node.querySelectorAll('.variant-selector__label')
                    ].map(el => clean(el.innerText));

                    if (
                        labels.some(text =>
                            /^(?:Vælg\\s+)?Farve\\b/i.test(text)
                        )
                    ) {
                        isColorGroup = true;
                        break;
                    }
                }

                if (!isColorGroup) continue;

                for (const link of group.querySelectorAll("a[href*='/p-']")) {
                    const colorNode = link.querySelector(
                        'div.d-flex.align-items-center'
                    );

                    results.push({
                        href: link.getAttribute('href') || '',
                        color: clean(colorNode?.innerText || link.innerText)
                    });
                }
            }

            return results;
        }"""
    )

    for option in color_options:
        href = option.get("href", "")
        if not href:
            continue

        variant_url = urljoin(page.url, href)
        if is_dame_url(variant_url):
            continue

        key = variant_url.rstrip("/")
        if key in seen_urls:
            continue

        seen_urls.add(key)
        variants.append((variant_url, clean(option.get("color", ""))))

    # Clothing products may use the separate color strip.
    if not variants:
        old_color_links = page.locator(
            "#product-variants a[href*='/p-']"
        )

        for index in range(old_color_links.count()):
            link = old_color_links.nth(index)
            href = link.get_attribute("href")
            if not href:
                continue

            variant_url = urljoin(page.url, href)
            if is_dame_url(variant_url):
                continue

            key = variant_url.rstrip("/")
            if key in seen_urls:
                continue

            try:
                color_name = clean(link.inner_text())
            except Exception:
                color_name = ""

            seen_urls.add(key)
            variants.append((variant_url, color_name))

    # When there are no color links, treat the current page as the only color.
    if not variants and not is_dame_url(page.url):
        variants.append((page.url, get_color_name(page, "")))

    # Some selectors omit the currently selected SKU. Keep it if it was not
    # already included among the links.
    if variants and not is_dame_url(page.url):
        current_key = page.url.rstrip("/")
        if current_key not in seen_urls and "/p-" in page.url:
            variants.insert(0, (page.url, get_color_name(page, "")))

    return variants


def get_color_name(page, fallback: str) -> str:
    """Get the current color from the product selector."""
    main = page.locator("main").first

    for label in main.locator(".variant-selector__label").all():
        text = clean(label.inner_text())
        match = re.match(r"^Farve:\s*(.*)$", text, re.I)
        if match:
            color = re.split(
                r"\s+(?:Størrelse|Lagerstatus):",
                match.group(1),
                maxsplit=1,
                flags=re.I,
            )[0].strip()
            if color:
                return color

    # On some color selectors, the selected option's visible text is the
    # color name even when the label itself is hidden or incomplete.
    selected_option = main.locator(
        ".variant-selector__items .size-variant--selected "
        "div.d-flex.align-items-center"
    ).first
    if selected_option.count():
        selected_text = clean(selected_option.inner_text())
        if selected_text:
            return selected_text

    return clean(fallback)


def unique_size_options(
    options: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Remove duplicate size URLs while keeping their original order."""
    result = []
    seen = set()

    for size_url, label in options:
        key = size_url.rstrip("/")
        if key not in seen:
            seen.add(key)
            result.append((size_url, label))

    return result


def get_size_options(page) -> list[tuple[str, str]]:
    """Find enabled size options and reject color names that resemble sizes."""
    main = page.locator("main").first
    size_pattern = re.compile(
        r"^(?:"
        r"W\d+\s*/\s*L\d+|"
        r"\d{2,3}\s*[-–]\s*\d{2,3}|"
        r"\d{1,3}(?:[.,]\d{1,2}|[⅓⅔½¼¾])?(?:\s+\d/\d)?|"
        r"\d{1,2}(?:[.,]\d{1,2})?\s*(?:UK|US|EU|DK)|"
        r"XXS|XS|S|M|L|XL|XXL|XXXL|XXXXL|4XL|5XL|"
        r"S/M|M/L|L/XL|"
        r"ONE[\s-]?SIZE"
        r")$",
        re.I,
    )

    def is_size_label(value: str) -> bool:
        return bool(size_pattern.fullmatch(clean(value)))

    selected_size_label = ""
    has_size_selector_label = False

    for label in main.locator(".variant-selector__label:visible").all():
        text = clean(label.inner_text())

        if re.match(r"^Vælg størrelse\b", text, re.I):
            has_size_selector_label = True
            continue

        match = re.match(r"^Størrelse:\s*(.+)$", text, re.I)
        if match:
            has_size_selector_label = True
            candidate = clean(match.group(1))
            if is_size_label(candidate) and not selected_size_label:
                selected_size_label = candidate

    options = []

    # Link-style sizes. Only enabled links are included.
    size_links = main.locator(
        ".variant-selector__items "
        ".size-variant:not(.size-variant--disabled) "
        "a.size-variant__inner[href*='/p-']"
    )

    for link in size_links.all():
        href = link.get_attribute("href")
        label = clean(link.inner_text())

        if href and is_size_label(label):
            size_url = urljoin(BASE_URL, href)
            if not is_dame_url(size_url):
                options.append((size_url, label))

    if options:
        if (
            selected_size_label
            and all(
                label.casefold() != selected_size_label.casefold()
                for _, label in options
            )
        ):
            options.insert(0, (page.url, selected_size_label))

        return unique_size_options(options)

    # Some products use a dropdown for their sizes.
    dropdown = main.locator(".dropdown__trigger").first
    if dropdown.count() and has_size_selector_label:
        dismiss_cookie_banner(page)

        try:
            dropdown.click(timeout=2500)
        except Exception:
            pass

        for link in main.locator("a[href*='/p-']").all():
            href = link.get_attribute("href")
            label = clean(link.inner_text())

            if href and is_size_label(label):
                size_url = urljoin(BASE_URL, href)
                if not is_dame_url(size_url):
                    options.append((size_url, label))

        selected = main.locator(
            ".dropdown__trigger .variant-container"
        ).first
        if selected.count():
            selected_label = clean(selected.inner_text())
            if (
                is_size_label(selected_label)
                and all(
                    label.casefold() != selected_label.casefold()
                    for _, label in options
                )
            ):
                options.insert(0, (page.url, selected_label))

        if (
            selected_size_label
            and all(
                label.casefold() != selected_size_label.casefold()
                for _, label in options
            )
        ):
            options.insert(0, (page.url, selected_size_label))

        if options:
            return unique_size_options(options)

    if selected_size_label:
        return [(page.url, selected_size_label)]

    if re.search(r"\bone[\s-]?size\b", clean(main.inner_text()), re.I):
        return [(page.url, "One size")]

    return []


def check_store_stock(page, store_name: str) -> tuple[str, str]:
    """Read the requested store's stock status from Salling's product page."""
    dismiss_cookie_banner(page)

    main = page.locator("main").first
    page_text = clean(main.inner_text())

    if re.search(r"udsolgt i alle butikker|udsolgt i alle", page_text, re.I):
        return "out_of_stock", "Udsolgt i alle butikker"

    # If the page only says stock is online, treat it as out of stock at the
    # requested store unless the text also reports a number of stores.
    online_match = re.search(
        r"\b(?:få\s+)?på lager online\b",
        page_text,
        re.I,
    )
    if online_match:
        availability_text = page_text[
            online_match.start():min(len(page_text), online_match.end() + 120)
        ]
        has_store_count = re.search(
            r"\bi\s+\d+\s+butikker?\b",
            availability_text,
            re.I,
        )
        if not has_store_count:
            return (
                "out_of_stock",
                "Online stock shown, but no 'i N butikker' store indicator "
                "was shown",
            )

    button_selector = "button.reset-button:visible:has(u)"
    store_button = page.locator(button_selector).filter(
        has_text=re.compile(r"\d+\s*butikker", re.I)
    )
    if not store_button.count():
        return "unknown", "Store availability button not found"

    popup_heading = page.locator("p.overlay__header").filter(
        has_text=re.compile(r"^\s*Lagerstatus\s*$", re.I)
    )
    last_click_error = ""

    for attempt in range(1, 4):
        dismiss_cookie_banner(page)

        store_button = page.locator(button_selector).filter(
            has_text=re.compile(r"\d+\s*butikker", re.I)
        )
        if not store_button.count():
            return "unknown", "Store availability button disappeared"

        button = store_button.first
        count_text = button.locator("u").first

        try:
            button.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        try:
            if count_text.count():
                count_text.click(timeout=2500)
            else:
                button.click(timeout=2500)
        except Exception as exc:
            last_click_error = str(exc)
            try:
                if count_text.count():
                    count_text.click(force=True, timeout=1500)
                else:
                    button.click(force=True, timeout=1500)
            except Exception as forced_exc:
                last_click_error = str(forced_exc)

        try:
            popup_heading.wait_for(state="visible", timeout=3500)
            break
        except Exception:
            dismiss_cookie_banner(page)

            if attempt == 3:
                try:
                    button_text = clean(button.inner_text())
                except Exception:
                    button_text = "(button text unavailable)"

                try:
                    visible_headers = [
                        clean(text)
                        for text in page.locator(
                            "p.overlay__header:visible"
                        ).all_text_contents()
                    ]
                except Exception:
                    visible_headers = []

                return "unknown", (
                    "Clicked the store-count control, but the Lagerstatus "
                    "popup did not become visible. "
                    f"Button text: {button_text!r}; "
                    f"visible overlay headings: {visible_headers!r}; "
                    f"last click error: {last_click_error}"
                )

    popup = popup_heading.locator(
        "xpath=ancestor::div[contains("
        "concat(' ', normalize-space(@class), ' '), ' overlay ')][1]"
    )
    rows = popup.locator("div.store")

    try:
        rows.first.wait_for(state="visible", timeout=8000)
    except Exception:
        close_stock_panel(page)
        return (
            "unknown",
            "Lagerstatus heading appeared, but store rows did not load",
        )

    for row in rows.all():
        store_label = row.locator("p.text-medium").first
        if not store_label.count():
            continue

        if clean(store_label.inner_text()).casefold() != store_name.casefold():
            continue

        row_text = clean(row.inner_text())

        if re.search(r"\bFå på lager\b", row_text, re.I):
            result = ("few_in_stock", "Få på lager")
        elif re.search(r"\bPå lager\b", row_text, re.I):
            result = ("in_stock", "På lager")
        elif re.search(r"\bUdsolgt\b|\bIkke på lager\b", row_text, re.I):
            result = ("out_of_stock", "Udsolgt")
        else:
            result = ("unknown", "No recognized stock label in store row")

        close_stock_panel(page)
        return result

    close_stock_panel(page)
    return "unknown", f"Store '{store_name}' was not listed in the Lagerstatus popup"


def summarize_stock(
    in_stock: list[str],
    few_in_stock: list[str],
    out_of_stock: list[str],
    unknown: list[dict],
) -> str:
    """Summarize checked sizes without hiding unknown checks."""
    if in_stock:
        return "in_stock"
    if few_in_stock:
        return "few_in_stock"
    if unknown:
        return "unknown"
    if out_of_stock:
        return "out_of_stock"
    return "unknown"


def test_product_on_page(
    page,
    url: str,
    product_name: str,
    store: str = "Salling Aarhus",
) -> dict:
    """Test all colors and enabled sizes using the caller's existing page."""
    records = []
    errors = []
    tested_size_urls = []

    if is_dame_url(url):
        return {
            "records": [],
            "errors": [{
                "url": url,
                "product_name": clean_product_name(product_name),
                "error": "Skipped because the URL contains 'dame'",
                "stage": "url_filter",
            }],
            "tested_size_urls": [],
        }

    try:
        open_product(page, url)
        shared = product_shared(page, product_name, url)
        variants = get_color_variants(page)

        print(f"Color variants found: {len(variants)}")

        for variant_url, variant_label in variants:
            try:
                open_product(page, variant_url)
                color = get_color_name(page, variant_label)

                thumbnails = page.locator(
                    "#product-images-thumbnails img"
                )
                try:
                    thumbnails.first.wait_for(state="visible", timeout=3000)
                except Exception:
                    pass

                image_urls = thumbnails.evaluate_all(
                    "imgs => [...new Set(imgs.map(img => "
                    "img.currentSrc || img.src).filter(Boolean))]"
                )

                sizes = get_size_options(page)
                if not sizes:
                    errors.append({
                        "url": variant_url,
                        "color": color,
                        "error": "No selectable size options were found",
                        "stage": "size_discovery",
                    })
                    continue

                print(
                    f"Checking color {color or '(unknown)'}: "
                    f"{len(sizes)} size option(s)"
                )

                in_stock = []
                few_in_stock = []
                out_of_stock = []
                unknown = []

                for size_url, size_label in sizes:
                    if size_url not in tested_size_urls:
                        tested_size_urls.append(size_url)

                    try:
                        if page.url.rstrip("/") != size_url.rstrip("/"):
                            open_product(page, size_url)

                        status, reason = check_store_stock(page, store)

                        if status == "in_stock":
                            in_stock.append(size_label)
                        elif status == "few_in_stock":
                            few_in_stock.append(size_label)
                        elif status == "out_of_stock":
                            out_of_stock.append(size_label)
                        else:
                            unknown.append({
                                "size": size_label,
                                "reason": reason,
                            })

                    except Exception as exc:
                        unknown.append({
                            "size": size_label,
                            "reason": str(exc),
                        })
                        errors.append({
                            "url": size_url,
                            "color": color,
                            "size": size_label,
                            "error": str(exc),
                            "stage": "size_check",
                        })

                record = dict(shared)
                record.update({
                    "url": variant_url,
                    "color": color,
                    "sizes_in_stock": in_stock,
                    "sizes_few_in_stock": few_in_stock,
                    "sizes_out_of_stock": out_of_stock,
                    "availability_unknown": unknown,
                    "image_urls": image_urls,
                    "stock": summarize_stock(
                        in_stock,
                        few_in_stock,
                        out_of_stock,
                        unknown,
                    ),
                    "store": store,
                })
                records.append(record)

            except Exception as exc:
                errors.append({
                    "url": variant_url,
                    "color": variant_label,
                    "error": str(exc),
                    "stage": "color_check",
                })

    except Exception as exc:
        errors.append({
            "url": url,
            "product_name": clean_product_name(product_name),
            "error": str(exc),
            "stage": "product_setup",
        })

    return {
        "records": records,
        "errors": errors,
        "tested_size_urls": tested_size_urls,
    }


def test_product(
    url: str,
    product_name: str,
    store: str = "Salling Aarhus",
    headless: bool = True,
) -> dict:
    """Run one product test in a new browser context."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(locale="da-DK")
        page = context.new_page()

        try:
            return test_product_on_page(page, url, product_name, store)
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("product_name")
    parser.add_argument("--store", default="Salling Aarhus")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    result = test_product(
        args.url,
        args.product_name,
        store=args.store,
        headless=not args.headed,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
