#!/usr/bin/env python3
"""Read shared Magasin product details and store pickup stock by color and size.

Selectors are based on the Magasin markup supplied in this conversation.
Use --debug to print diagnostic details for errors.
"""
from __future__ import annotations

import argparse
import json
import re
from contextlib import nullcontext
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)


COOKIE = "#declineButton"
PRODUCT_NAME = "main h1"
COLOR_RADIOS = '[role="radio"][aria-label*="Vælg Farve"]'
COLOR_LABEL = "#color-swatch-label .js-swatchName"
PICKUP_BUTTON = "button.stock-availability__button.js-stock-availability-modal"
OVERLAY = ".stock-availability-overlay"
OVERLAY_SIZE = (
    ".stock-availability-overlay__size-selection "
    "select.js-stock-size-select"
)
STORE_ROWS = "li.stock-availability-overlay__item"
GALLERY = "div.b-carousel__slides.product-carousel__scroll"


def _text(locator) -> str:
    """Read normalized text from the first match, or return an empty string."""
    try:
        if locator.count():
            return " ".join(locator.first.inner_text(timeout=2500).split())
    except Exception:
        pass
    return ""


def _attr(locator, name: str) -> str:
    """Read an HTML attribute from a locator, or return an empty string."""
    try:
        return locator.get_attribute(name, timeout=1500) or ""
    except Exception:
        return ""


def _click_cookie(page: Page) -> None:
    """Click the supplied 'Kun nødvendige' button if the cookie banner appears."""
    button = page.locator(COOKIE)
    try:
        button.wait_for(state="visible", timeout=1800)
        button.click(timeout=3000)
        button.wait_for(state="hidden", timeout=3000)
    except PlaywrightTimeoutError:
        # A saved consent choice means the banner may not appear on this visit.
        pass


def _read_specifications(page: Page) -> dict[str, str]:
    """Read the currently displayed product specification rows."""
    specifications: dict[str, str] = {}

    rows = page.locator(
        "main dl.b-descriptionlist.pdp-specifications "
        ".b-descriptionlist__item"
    )

    for row in rows.all():
        term = _text(row.locator("dt.b-descriptionlist__item-name"))
        value = _text(row.locator("dd.b-descriptionlist__item-value"))

        if term:
            specifications[term.strip().rstrip(":")] = value

    return specifications


def _get_shared(
    page: Page,
    url: str,
    supplied_name: str,
) -> dict[str, Any]:
    """Read product fields that are shared across colors."""
    name = _text(page.locator(PRODUCT_NAME)) or supplied_name
    brand = _text(page.locator('main a[href*="/maerker/"]').first)

    # Limit price parsing to the product summary near the h1 so prices from
    # recommendation cards are not accidentally included.
    price_block = _text(
        page.locator(PRODUCT_NAME).locator("xpath=../..")
    )
    prices = re.findall(
        r"\d[\d.]*,\d{2}\s*kr\.?|\d[\d.]*\s*kr\.?",
        price_block,
    )

    price = prices[0].replace("\xa0", " ") if prices else ""
    original_price = (
        prices[1].replace("\xa0", " ")
        if len(prices) > 1
        else ""
    )
    on_sale = bool(original_price and price != original_price)
    sale_percentage = None

    if on_sale:
        def as_number(value: str) -> float:
            cleaned = (
                value.lower()
                .replace("kr", "")
                .replace(" ", "")
                .replace(".", "")
                .replace(",", ".")
            )
            return float(cleaned)

        try:
            sale_percentage = round(
                (1 - as_number(price) / as_number(original_price)) * 100
            )
        except (ValueError, ZeroDivisionError):
            pass

    # Fit is treated as shared. The full specifications are read again after
    # each color is selected because some specification values are color-specific.
    specifications = _read_specifications(page)

    breadcrumbs = page.locator(
        'main nav a, main [aria-label*="Breadcrumb"] a'
    ).all_text_contents()
    product_type = breadcrumbs[-1].strip() if breadcrumbs else ""

    return {
        "product_name": name,
        "product_type": product_type,
        "brand": brand,
        "fit": specifications.get("Pasform", ""),
        "price": price,
        "original_price": original_price,
        "on_sale": on_sale,
        "sale_percentage": sale_percentage,
        "url": url,
    }


def _get_gallery_image_urls(page: Page) -> list[str]:
    """Return Magasin image URLs from the currently displayed product gallery."""
    image_urls: list[str] = []
    gallery = page.locator(GALLERY)

    for image in gallery.locator("img[src], img[data-src]").all():
        # Lazy-loaded images may have a placeholder in src and the actual image
        # URL in data-src, so examine both attributes.
        for src in (_attr(image, "src"), _attr(image, "data-src")):
            if (
                "www.magasin.dk/dw/image" in src
                and src not in image_urls
            ):
                image_urls.append(src)
                break

    return image_urls


def _read_facts(page: Page) -> dict[str, str]:
    """Expand the Fakta section and read its EAN-identified description list.

    The EAN requirement distinguishes the Fakta list from other description
    lists. Values such as EAN, size, color, SKU, and ID can vary by variant.
    """
    def facts_list_has_ean() -> bool:
        return bool(
            page.evaluate(
                r"""() => {
                    return [...document.querySelectorAll(
                        'main div.inner.ih-p-0 dl.b-descriptionlist'
                    )].some(dl => [...dl.querySelectorAll('dt')].some(dt =>
                        dt.textContent.trim().replace(/[:\s]+$/, '') === 'EAN'
                    ));
                }"""
            )
        )

    if not facts_list_has_ean():
        facts_button = page.get_by_role(
            "button",
            name="Fakta",
            exact=True,
        )
        if facts_button.count() and facts_button.first.is_visible():
            facts_button.first.click(timeout=5000)

        page.wait_for_function(
            r"""() => {
                return [...document.querySelectorAll(
                    'main div.inner.ih-p-0 dl.b-descriptionlist'
                )].some(dl => [...dl.querySelectorAll('dt')].some(dt =>
                    dt.textContent.trim().replace(/[:\s]+$/, '') === 'EAN'
                ));
            }""",
            timeout=8000,
        )

    # Read the description list whose labels include EAN.
    return page.evaluate(
        r"""() => {
            const lists = [...document.querySelectorAll(
                'main div.inner.ih-p-0 dl.b-descriptionlist'
            )];
            const facts = lists.find(dl => [...dl.querySelectorAll('dt')].some(dt =>
                dt.textContent.trim().replace(/[:\s]+$/, '') === 'EAN'
            ));

            if (!facts) return {};

            const result = {};
            for (const row of facts.querySelectorAll('.b-descriptionlist__item')) {
                const term = row.querySelector('dt')?.textContent
                    .trim().replace(/[:\s]+$/, '');
                const value = row.querySelector('dd')?.innerText
                    .replace(/\s+/g, ' ').trim();

                if (term) result[term] = value || '';
            }
            return result;
        }"""
    )


def _color_names(page: Page) -> list[str]:
    """Return unique color labels from the page's color radio controls."""
    names: list[str] = []

    for radio in page.locator(COLOR_RADIOS).all():
        label = _attr(radio, "aria-label")
        label = label.replace("Vælg Farve", "", 1).strip()

        if label and label not in names:
            names.append(label)

    if names:
        return names

    current_color = _text(page.locator(COLOR_LABEL))
    return [current_color] if current_color else [""]


def _select_color(page: Page, color: str) -> None:
    """Select a color and wait for its label and gallery to refresh."""
    if not color:
        return

    radios = page.locator(COLOR_RADIOS)
    target_index = None
    available = []

    for index in range(radios.count()):
        radio = radios.nth(index)
        aria_label = _attr(radio, "aria-label").strip()
        data_color = _attr(radio, "data-attr-value").strip()
        label_color = aria_label.replace("Vælg Farve", "", 1).strip()

        available.append(
            {
                "aria_label": aria_label,
                "data_attr_value": data_color,
                "checked": _attr(radio, "aria-checked"),
            }
        )

        if (
            label_color.casefold() == color.casefold()
            or data_color.casefold() == color.casefold()
        ):
            target_index = index

    if target_index is None:
        current_color = _text(page.locator(COLOR_LABEL))
        if current_color.casefold() == color.casefold():
            return

        raise RuntimeError(
            f"Color {color!r} not found. "
            f"Current color={current_color!r}; "
            f"available swatches={available!r}"
        )

    target = radios.nth(target_index)

    # The page may already be showing this color.
    if _attr(target, "aria-checked").casefold() == "true":
        return

    previous_gallery = _get_gallery_image_urls(page)

    # The observed markup puts the clickable control inside the radio <li>.
    swatch_button = target.locator("button.js-swatch-value")
    if swatch_button.count():
        swatch_button.first.click(timeout=8000)
    else:
        target.click(timeout=8000)

    # Match by color identity rather than by list index: swatches can rerender.
    page.wait_for_function(
        """expected => {
            const normalize = value =>
                (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();

            const wanted = normalize(expected);
            const label = document.querySelector(
                '#color-swatch-label .js-swatchName'
            )?.textContent;

            if (normalize(label) === wanted) return true;

            return [...document.querySelectorAll(
                '[role="radio"][aria-label*="Vælg Farve"]'
            )].some(radio => {
                const ariaColor = normalize(
                    (radio.getAttribute('aria-label') || '')
                        .replace('Vælg Farve', '')
                );
                const dataColor = normalize(
                    radio.getAttribute('data-attr-value')
                );

                return radio.getAttribute('aria-checked') === 'true'
                    && (ariaColor === wanted || dataColor === wanted);
            });
        }""",
        arg=color,
        timeout=12000,
    )

    # The color label can change just before the carousel does. Wait briefly
    # for a gallery image URL to change; some colors may share gallery images.
    if previous_gallery:
        try:
            page.wait_for_function(
                """previous => {
                    const gallery = document.querySelector(
                        'div.b-carousel__slides.product-carousel__scroll'
                    );
                    if (!gallery) return false;

                    const current = [...gallery.querySelectorAll('img')]
                        .map(img =>
                            img.getAttribute('src') || img.dataset.src || ''
                        )
                        .filter(Boolean);

                    return current.some(src => !previous.includes(src));
                }""",
                arg=previous_gallery,
                timeout=7000,
            )
        except PlaywrightTimeoutError:
            # Continue if this color shares the same gallery assets.
            pass


def _open_pickup(page: Page) -> None:
    """Open store availability, including products that first ask for a size."""
    overlay = page.locator(OVERLAY)

    if overlay.count() and overlay.first.is_visible():
        return

    def wait_for_overlay(timeout_ms: int) -> bool:
        try:
            overlay.first.wait_for(
                state="visible",
                timeout=timeout_ms,
            )
            return True
        except PlaywrightTimeoutError:
            return False

    def click_pickup_button() -> None:
        button = page.locator(PICKUP_BUTTON).first
        button.wait_for(state="visible", timeout=12000)
        button.scroll_into_view_if_needed(timeout=4000)

        # Wait for the button to contain its current PID and stock endpoint.
        page.wait_for_function(
            """selector => {
                const button = document.querySelector(selector);
                return Boolean(
                    button
                    && button.isConnected
                    && !button.disabled
                    && button.getAttribute('data-pid')
                    && button.getAttribute('data-stockurl')
                );
            }""",
            arg=PICKUP_BUTTON,
            timeout=10000,
        )
        button.click(timeout=7000)

    click_pickup_button()

    # Most products open the pickup panel directly. Some show a size dialog first.
    if wait_for_overlay(5000):
        return

    size_modal = page.locator(
        'custom-modal[data-modal-name="size-selection-modal"]'
    )

    try:
        size_modal.wait_for(state="visible", timeout=3000)
    except PlaywrightTimeoutError:
        # Support the other size modal structure observed on Magasin.
        size_modal = page.locator(
            "div.modal__inner "
            "size-variation-selection.b-pdp__size-modal-selection"
        )
        try:
            size_modal.wait_for(state="visible", timeout=1000)
        except PlaywrightTimeoutError:
            size_modal = None

    if size_modal is not None:
        # Size attributes and click handling are on the button in the observed
        # modal markup, not on the surrounding list item.
        size_buttons = size_modal.locator(
            "button.js-attribute-change-stock[data-variant-pid]"
        )
        selectable = []

        for candidate in size_buttons.all():
            if not candidate.is_enabled():
                continue
            if _attr(candidate, "data-selectable").casefold() == "false":
                continue
            selectable.append(candidate)

        if not selectable:
            raise RuntimeError(
                "Magasin's size-selection modal opened but contained no enabled "
                "size buttons. "
                f"Modal text={_text(size_modal)!r}; "
                f"button count={size_buttons.count()}"
            )

        # This first size selection unlocks the stock panel. The code in
        # scrape_product() then checks every size in the stock panel selector.
        first_size = selectable[0]
        chosen_size = (
            _attr(first_size, "data-attr-value")
            or _text(first_size)
        ).strip()
        chosen_pid = _attr(first_size, "data-variant-pid").strip()

        first_size.click(timeout=8000)

        # Choosing a size may open the stock panel automatically.
        if wait_for_overlay(10000):
            return

        # Otherwise, wait for the chooser to close and click pickup again.
        try:
            size_modal.wait_for(state="hidden", timeout=6000)
        except PlaywrightTimeoutError:
            if not (overlay.count() and overlay.first.is_visible()):
                raise RuntimeError(
                    "Clicked a size in Magasin's size-selection modal, but the "
                    "modal stayed open and the stock panel did not appear. "
                    f"Size={chosen_size!r}, PID={chosen_pid!r}, "
                    f"modal text={_text(size_modal)!r}"
                )

        if wait_for_overlay(1000):
            return

        click_pickup_button()

        if wait_for_overlay(15000):
            return

        raise RuntimeError(
            "Selected a size and retried the pickup button, but the store "
            "stock panel did not open. "
            f"Size={chosen_size!r}, PID={chosen_pid!r}, URL={page.url!r}"
        )

    # The size prompt is not always shown. If there is no modal, use an inline
    # selectable size only when the product page has no size selected yet.
    selected_size = page.locator(
        'main li.b-swatch--size[role="radio"][aria-checked="true"]'
    )
    inline_sizes = page.locator(
        'main li.b-swatch--size[role="radio"][data-selectable="true"]'
    )

    if not selected_size.count() and inline_sizes.count():
        inline_button = inline_sizes.first.locator("button").first
        if inline_button.count():
            inline_button.click(timeout=8000)

            if wait_for_overlay(8000):
                return

    # A selected/default size may need another pickup click after the first request.
    click_pickup_button()

    if wait_for_overlay(15000):
        return

    diagnostic = page.evaluate(
        """selector => {
            const visible = el => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                return rect.width > 0 && rect.height > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none';
            };

            return {
                url: location.href,
                pickupButtons: [...document.querySelectorAll(selector)].map(el => ({
                    visible: visible(el),
                    disabled: el.disabled,
                    pid: el.getAttribute('data-pid'),
                    text: el.innerText.trim()
                })),
                sizeDialogs: [...document.querySelectorAll(
                    'custom-modal[data-modal-name="size-selection-modal"]'
                )].map(el => ({
                    visible: visible(el),
                    text: el.innerText.slice(0, 1000)
                })),
                overlays: [...document.querySelectorAll(
                    '.stock-availability-overlay'
                )].map(el => ({
                    visible: visible(el),
                    text: el.innerText.slice(0, 1000)
                })),
                pageTextEnd: document.body.innerText.slice(-1200)
            };
        }""",
        arg=PICKUP_BUTTON,
    )

    raise RuntimeError(
        "The pickup panel did not open, and no size chooser could be completed. "
        + json.dumps(diagnostic, ensure_ascii=False)
    )


def _read_store(page: Page, store: str) -> dict[str, Any]:
    """Read a store's stock status and return diagnostics if it is not found."""
    overlay = page.locator(OVERLAY)

    try:
        overlay.wait_for(state="visible", timeout=7000)
        overlay_text = _text(overlay)
        panel_open = True
    except PlaywrightTimeoutError:
        overlay_text = _text(page.locator("body"))[-3000:]
        panel_open = False

    stores_found: list[str] = []

    # Only inspect store rows inside the visible pickup overlay.
    for row in overlay.locator(STORE_ROWS).all():
        store_name = _text(
            row.locator(".stock-availability-overlay__item-name")
        )

        if store_name and store_name not in stores_found:
            stores_found.append(store_name)

        if store_name.casefold() != store.casefold():
            continue

        message = _text(row.locator("p.availability-message"))
        lowered = message.casefold()

        # Check negative signals first, then the specific limited-stock phrase.
        # "Få på lager" also contains the generic words "på lager".
        if any(
            phrase in lowered
            for phrase in (
                "udsolgt",
                "ikke på lager",
                "kan ikke afhentes",
                "ingen varer",
                "ingen på lager",
                "ikke tilgængelig",
            )
        ):
            status = "out_of_stock"
        elif "få på lager" in lowered:
            status = "few_in_stock"
        elif "på lager" in lowered:
            status = "in_stock"
        else:
            status = "unknown"

        return {
            "status": status,
            "message": message,
            "diagnostic": _text(row),
            "panel_open": panel_open,
            "stores_found": stores_found,
        }

    lowered_panel = overlay_text.casefold()
    sold_out_everywhere = re.search(
        r"udsolgt i alle forretninger|"
        r"ikke på lager i nogen forretninger|"
        r"udsolgt i alle butikker",
        lowered_panel,
    )

    return {
        "status": (
            "sold_out_all_stores"
            if sold_out_everywhere
            else "store_not_found"
        ),
        "message": "",
        "diagnostic": overlay_text[-3000:],
        "panel_open": panel_open,
        "stores_found": stores_found,
    }


def _close_pickup(page: Page) -> None:
    """Close the pickup panel using its accessible 'Luk' button if present."""
    overlay = page.locator(OVERLAY)

    if not overlay.count() or not overlay.first.is_visible():
        return

    close_button = overlay.get_by_role(
        "button",
        name="Luk",
        exact=True,
    )

    if close_button.count() and close_button.first.is_visible():
        close_button.first.click(timeout=4000)
    else:
        page.keyboard.press("Escape")

    try:
        overlay.wait_for(state="hidden", timeout=4000)
    except PlaywrightTimeoutError:
        # _reset_for_color() reloads the page if the panel remains mounted.
        pass


def _reset_for_color(page: Page, url: str, color: str) -> None:
    """Reload the product between colors, then select the requested color."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.locator(PRODUCT_NAME).wait_for(
        state="visible",
        timeout=20000,
    )
    _click_cookie(page)
    _select_color(page, color)


def scrape_product(
    url: str,
    product_name: str = "",
    store: str = "Magasin Aarhus",
    headless: bool = True,
    page: Page | None = None,
) -> dict[str, Any]:
    """Return one product record per color, reusing a caller-supplied Page.

    The link tester passes its existing Page so the crawl stays in one browser
    tab and context. When run from the command line, this function creates and
    closes its own browser.
    """
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    owns_browser = page is None
    playwright_context = (
        sync_playwright()
        if owns_browser
        else nullcontext(None)
    )

    with playwright_context as playwright:
        browser = None

        if owns_browser:
            browser = playwright.chromium.launch(headless=headless)
            page = browser.new_page(locale="da-DK")

        assert page is not None

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.locator(PRODUCT_NAME).wait_for(
                state="visible",
                timeout=20000,
            )
            _click_cookie(page)

            # Product-level details are read once.
            shared = _get_shared(page, url, product_name)
            colors = _color_names(page)

            for color_index, color in enumerate(colors):
                sizes_in_stock: list[str] = []
                sizes_few_in_stock: list[str] = []
                sizes_out_of_stock: list[str] = []

                try:
                    if color_index == 0:
                        _select_color(page, color)
                    else:
                        _reset_for_color(page, url, color)

                    # These can change with the selected color, so read them now.
                    color_image_urls = _get_gallery_image_urls(page)
                    color_specifications = _read_specifications(page)

                    # Fakta may include the currently selected size and color.
                    try:
                        facts = _read_facts(page)
                    except Exception as facts_error:
                        facts = {}
                        errors.append(
                            {
                                "url": url,
                                "color": color,
                                "stage": "facts",
                                "error": str(facts_error),
                            }
                        )

                    _open_pickup(page)

                    # Scope the size selector to the pickup overlay. The product
                    # page may have a separate size control.
                    size_select = (
                        page.locator(OVERLAY).locator(OVERLAY_SIZE)
                    )
                    size_select.wait_for(
                        state="visible",
                        timeout=8000,
                    )

                    options = size_select.locator("option").evaluate_all(
                        """options => options
                            .filter(option => option.value && !option.disabled)
                            .map(option => ({
                                label: option.textContent.trim(),
                                value: option.value
                            }))
                        """
                    )

                    if not options:
                        check = _read_store(page, store)

                        if check["status"] in {
                            "in_stock",
                            "few_in_stock",
                        }:
                            sizes_in_stock.append("unspecified")
                        elif check["status"] in {
                            "out_of_stock",
                            "sold_out_all_stores",
                        }:
                            sizes_out_of_stock.append("unspecified")
                        else:
                            errors.append(
                                {
                                    "url": url,
                                    "color": color,
                                    "stage": "store_panel",
                                    "error": f"Could not locate/read {store!r}",
                                    "requested_store": store,
                                    **check,
                                }
                            )

                    for option in options:
                        size = option["label"]
                        size_url = option["value"]
                        pid_match = re.search(
                            r"[?&]pid=([^&]+)",
                            size_url,
                        )
                        size_pid = (
                            pid_match.group(1)
                            if pid_match
                            else ""
                        )

                        overlay = page.locator(OVERLAY)

                        try:
                            if not size_pid:
                                raise RuntimeError(
                                    f"Size option has no pid: {size_url}"
                                )

                            # Changing this select requests stock for that size.
                            # The option URL is the site's Product-StockAvailability
                            # endpoint for the selected size.
                            selected_before = size_select.input_value()

                            if selected_before != size_url:
                                with page.expect_response(
                                    lambda response: (
                                        "Product-StockAvailability"
                                        in response.url
                                        and parse_qs(
                                            urlparse(response.url).query
                                        ).get("pid", [""])[0] == size_pid
                                    ),
                                    timeout=15000,
                                ) as stock_response:
                                    size_select.select_option(
                                        label=size,
                                        timeout=7000,
                                    )

                                response = stock_response.value
                                if not response.ok:
                                    raise RuntimeError(
                                        "Stock request returned HTTP "
                                        f"{response.status}: {response.url}"
                                    )

                            if size_select.input_value() != size_url:
                                raise RuntimeError(
                                    f"Size selector did not retain option URL "
                                    f"for {size}: "
                                    f"{size_select.input_value()!r}"
                                )

                            # The row may be absent for a globally sold-out size.
                            try:
                                (
                                    page.locator(OVERLAY)
                                    .locator(STORE_ROWS)
                                    .filter(
                                        has_text=re.compile(
                                            re.escape(store),
                                            re.I,
                                        )
                                    )
                                    .first.wait_for(
                                        state="visible",
                                        timeout=4000,
                                    )
                                )
                            except PlaywrightTimeoutError:
                                pass

                            check = _read_store(page, store)
                            check["selected_size_pid"] = size_pid
                            check["selected_size_label"] = size
                            check["selected_option_url"] = size_url

                            status = check["status"]

                            if status == "in_stock":
                                sizes_in_stock.append(size)
                            elif status == "few_in_stock":
                                sizes_few_in_stock.append(size)
                            elif status in {
                                "out_of_stock",
                                "sold_out_all_stores",
                            }:
                                sizes_out_of_stock.append(size)
                            else:
                                errors.append(
                                    {
                                        "url": url,
                                        "color": color,
                                        "size": size,
                                        "size_pid": size_pid,
                                        "stage": "store_panel",
                                        "error": (
                                            f"Could not locate/read {store!r}"
                                        ),
                                        "requested_store": store,
                                        **check,
                                    }
                                )

                        except Exception as size_error:
                            try:
                                overlay_visible = overlay.is_visible()
                            except Exception:
                                overlay_visible = False

                            try:
                                selected_size_value = size_select.input_value()
                            except Exception:
                                selected_size_value = ""

                            errors.append(
                                {
                                    "url": url,
                                    "color": color,
                                    "size": size,
                                    "size_pid": size_pid,
                                    "stage": "size_check",
                                    "error": str(size_error),
                                    "overlay_visible": overlay_visible,
                                    "selected_size_value": selected_size_value,
                                    "selected_option_url": size_url,
                                    "panel_text": _text(
                                        page.locator(OVERLAY)
                                    )[-2000:],
                                    "store_rows": [
                                        _text(row)
                                        for row in (
                                            page.locator(OVERLAY)
                                            .locator(STORE_ROWS)
                                            .all()
                                        )
                                    ],
                                }
                            )
                            sizes_out_of_stock.append(size)

                    _close_pickup(page)

                    if sizes_in_stock:
                        stock = "in_stock"
                    elif sizes_few_in_stock:
                        stock = "few_in_stock"
                    elif sizes_out_of_stock:
                        stock = "out_of_stock"
                    else:
                        stock = "unknown"

                    records.append(
                        {
                            **shared,
                            "specifications": color_specifications,
                            "image_urls": color_image_urls,
                            "fakta": facts,
                            "fakta_size": facts.get("Størrelse", ""),
                            "color": color,
                            "sizes_in_stock": sizes_in_stock,
                            "sizes_few_in_stock": sizes_few_in_stock,
                            "sizes_out_of_stock": sizes_out_of_stock,
                            "stock": stock,
                            "store": store,
                        }
                    )

                except Exception as variant_error:
                    try:
                        overlay_visible = page.locator(OVERLAY).is_visible()
                    except Exception:
                        overlay_visible = False

                    errors.append(
                        {
                            "url": url,
                            "color": color,
                            "stage": "variant",
                            "error": str(variant_error),
                            "pickup_button_count": page.locator(
                                PICKUP_BUTTON
                            ).count(),
                            "overlay_visible": overlay_visible,
                        }
                    )

                    # Try to leave a clean page state for the next color.
                    try:
                        _close_pickup(page)
                    except Exception:
                        pass

        except Exception as navigation_error:
            errors.append(
                {
                    "url": url,
                    "product_name": product_name,
                    "stage": "navigation_or_details",
                    "error": str(navigation_error),
                }
            )

        finally:
            # The caller owns a Page it passed in. Close only our own browser.
            if browser is not None:
                browser.close()

    return {
        "records": records,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--store", default="Magasin Aarhus")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print errors and panel diagnostics",
    )
    parser.add_argument("--output", default="-")
    args = parser.parse_args()

    result = scrape_product(
        args.url,
        args.name,
        args.store,
        headless=not args.show_browser,
    )

    if args.debug:
        for error in result["errors"]:
            print(json.dumps(error, ensure_ascii=False, indent=2))

    rendered = json.dumps(result, ensure_ascii=False, indent=2)

    if args.output == "-":
        print(rendered)
    else:
        with open(args.output, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")


if __name__ == "__main__":
    main()
