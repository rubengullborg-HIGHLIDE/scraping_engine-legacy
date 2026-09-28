import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


# EDIT HERE: Match the store name displayed on Quint's page.
STORE_NAME = "qUINT Bruuns Galleri"


# EDIT HERE: Add products here. Each needs a name and a Quint product URL.
PRODUCT_URL = (
    "https://www.quint.dk/produkt/non-sens-203257"
    "?color=0195041723507216a962d758300c2d51"
    "#color=0195041723507216a962d758300c2d51"
    "&from_cat=019623f9ceb772538ebb8a5716d521b6"
)
PRODUCTS = [
    {
        "name": "Calgary L/Æ T-shirt",
        "url": "https://www.quint.dk/produkt/non-sens-203257?color=0195041723507216a962d758300c2d51#color=0195041723507216a962d758300c2d51",
    },
    {
        "name": "Bukser",
        "url": "https://www.quint.dk/produkt/adidas-originals-202447?color=0195041720fe72b093f4b66c303cab8f#color=0195041720fe72b093f4b66c303cab8f&from_cat=01961fe574d873678962e5783009b3b8",
    },
    {
        "name": "Sko",
        "url": "https://www.quint.dk/produkt/sorel-202833?color=0195041723507216a962d758300c2d51#color=0195041723507216a962d758300c2d51&from_cat=019623f9eb6d73ba97d1313eb7134b5a",
    },
]

# EDIT HERE if your products use size formats that are not listed here.
SIZE_PATTERN = re.compile(
    r"^(XXS|XS|S|M|L|XL|XXL|XXXL|ONE SIZE|OS|"
    r"\d{2,3}(?:[.,]\d)?(?:\s+\d/\d)?|W\d{2}(?:/L?\d{2})?)$",
    re.IGNORECASE,
)


def extract_size_label(option_text):
    """Read a complete size at the start of an option label, including shoe fractions."""
    words = " ".join(option_text.split()).split()
    for word_count in range(min(3, len(words)), 0, -1):
        candidate = " ".join(words[:word_count])
        if SIZE_PATTERN.fullmatch(candidate):
            return candidate
    return ""


async def find_store_size_dropdown(panel):
    """Return the visible size dropdown, skipping hidden or unrelated buttons."""
    buttons = panel.locator("button.tw-border-ui-divider")
    for index in range(await buttons.count()):
        button = buttons.nth(index)
        if not await button.is_visible():
            continue
        label = " ".join((await button.inner_text()).split())
        if SIZE_PATTERN.fullmatch(label):
            return button
    return None


async def handle_cookie_prompt(page):
    """Click 'Kun nødvendige' if the cookie prompt appears."""
    decline_button = page.locator("#declineButton")

    try:
        await decline_button.wait_for(state="visible", timeout=5_000)
        await decline_button.click()
        await decline_button.wait_for(state="hidden", timeout=5_000)
    except PlaywrightTimeoutError:
        # The prompt may not appear, or may already have been dismissed.
        pass
async def get_color_image_urls(page):
            images = page.locator('main img[sizes="80px"][src]')
            image_urls = []

            for index in range(await images.count()):
                image_url = await images.nth(index).get_attribute("src")

                if image_url and image_url not in image_urls:
                    image_urls.append(image_url)

            return image_urls

async def expand_colors(page):
    reveal_buttons = page.locator("button").filter(
        has_text=re.compile(r"\+\s*\d+")
    )

    for index in range(await reveal_buttons.count()):
        button = reveal_buttons.nth(index)

        if await button.is_visible():
            await button.click()
            return

    # No visible "+ number" button: there are no hidden colors to expand,
    # or the colors are already expanded.
async def get_product_colors(page):
               await expand_colors(page)

               color_divs = page.locator(
                   "main div.tw-flex.tw-flex-wrap.tw-gap-2"
               )
               colors = []

               for div_index in range(await color_divs.count()):
                   buttons = color_divs.nth(div_index).locator("button.tw-inline-flex[title]")

                   for index in range(await buttons.count()):
                       button = buttons.nth(index)
                       color_name = (await button.get_attribute("title") or "").strip()

                       if color_name and color_name.casefold() not in {
                           color.casefold() for color in colors
                       }:
                           colors.append(color_name)

               return colors

async def select_color(page, color_name):
    """Click the product color swatch whose title matches color_name."""
    color_buttons = page.locator("main button.tw-inline-flex[title]")

    for index in range(await color_buttons.count()):
        button = color_buttons.nth(index)
        title = (await button.get_attribute("title") or "").strip()

        if title.casefold() == color_name.casefold():
            await button.click()
            return

    raise RuntimeError(f"Could not find a color swatch for {color_name!r}.")
async def check_color_sizes(page, color, product_info, image_urls):
    """Check every size for one color and return one aggregated color record."""
    find_button = page.locator("main button").filter(
        has_text=re.compile(r"^\s*Find i butik\s*$", re.IGNORECASE)
    ).filter(visible=True)

    if not await find_button.count():
        raise RuntimeError("Could not find the visible 'Find i butik' button.")

    await find_button.first.click()

    pickup_heading = page.get_by_role(
        "heading",
        name=re.compile(r"Hent nu i disse butikker", re.IGNORECASE),
    )
    delivery_heading = page.get_by_role(
        "heading",
        name=re.compile(r"Få din ordre leveret til en af disse butikker", re.IGNORECASE),
    )
    sold_out_message = page.get_by_text(
        re.compile(r"Produktet er desværre udsolgt i alle butikker", re.IGNORECASE)
    ).filter(visible=True)

    panel_heading = None
    sold_out = False
    for _ in range(50):
        if await sold_out_message.count():
            sold_out = True
            break
        if await pickup_heading.is_visible():
            panel_heading = pickup_heading
            break
        if await delivery_heading.is_visible():
            panel_heading = delivery_heading
            break
        await page.wait_for_timeout(200)

    record = {
        **product_info,
        "url": product_info["url"],
        "color": color,
        "sizes_in_stock": [],
        "sizes_few_in_stock": [],
        "stock": "out_of_stock",
        "image_urls": image_urls,
    }

    if sold_out:
        record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
        await page.keyboard.press("Escape")
        return record

    if panel_heading is None:
        raise RuntimeError(
            "The store window showed neither a sold-out message nor a pickup/delivery heading."
        )

    panel = panel_heading.locator("xpath=../../..")
    dropdown = await find_store_size_dropdown(panel)
    if dropdown is None:
        if await sold_out_message.count() and await sold_out_message.first.is_visible():
            record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
            await page.keyboard.press("Escape")
            return record
        raise RuntimeError(
            "The store panel opened, but no visible size dropdown was found. "
            "Check whether this product requires choosing a size on the product page first."
        )

    try:
        current_size = " ".join((await dropdown.inner_text()).split())
        try:
            await dropdown.click(timeout=5_000)
        except PlaywrightTimeoutError as exc:
            if await sold_out_message.count() and await sold_out_message.first.is_visible():
                record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
                await page.keyboard.press("Escape")
                return record
            raise RuntimeError(
                "The store panel's size dropdown was visible but could not be opened."
            ) from exc
        options = panel.locator("li.tw-cursor-pointer").filter(visible=True)

        sizes = []
        for index in range(await options.count()):
            option_text = " ".join((await options.nth(index).inner_text()).split())
            option_size = extract_size_label(option_text)
            if SIZE_PATTERN.fullmatch(option_size) and option_size.casefold() not in {
                item.casefold() for item in sizes
            }:
                sizes.append(option_size)

        try:
            await dropdown.click(timeout=5_000)
        except PlaywrightTimeoutError as exc:
            if await sold_out_message.count() and await sold_out_message.first.is_visible():
                record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
                record["sizes_not_checked"] = sizes
                await page.keyboard.press("Escape")
                return record
            raise RuntimeError(
                "The size menu was read, but the dropdown could not be closed."
            ) from exc

        if SIZE_PATTERN.fullmatch(current_size) and current_size.casefold() not in {
            item.casefold() for item in sizes
        }:
            sizes.insert(0, current_size)
        if not sizes:
            raise RuntimeError("The open store panel did not show any recognizable size options.")

        for size_index, size in enumerate(sizes):
            if size.casefold() != current_size.casefold():
                # The site may replace the store panel with an all-stores-sold-out
                # message when a selected variant has no pickup stock.
                if await sold_out_message.count() and await sold_out_message.first.is_visible():
                    record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
                    record["sizes_not_checked"] = sizes[size_index:]
                    break

                dropdown = await find_store_size_dropdown(panel)
                if dropdown is None:
                    if await sold_out_message.count() and await sold_out_message.first.is_visible():
                        record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
                        record["sizes_not_checked"] = sizes[size_index:]
                        break
                    raise RuntimeError(
                        f"The size dropdown disappeared before size {size!r} could be selected, "
                        "and the all-stores-sold-out message was not visible."
                    )

                try:
                    await dropdown.click(timeout=5_000)
                except PlaywrightTimeoutError as exc:
                    if await sold_out_message.count() and await sold_out_message.first.is_visible():
                        record["stock_note"] = "Produktet er desværre udsolgt i alle butikker"
                        record["sizes_not_checked"] = sizes[size_index:]
                        break
                    raise RuntimeError(
                        f"Could not reopen the size dropdown while checking {size!r}. "
                        "The store panel may have changed or closed."
                    ) from exc

                option = panel.locator("li.tw-cursor-pointer").filter(
                    has_text=re.compile(
                        rf"^\s*{re.escape(size)}(?:\s|$)", re.IGNORECASE
                    )
                ).filter(visible=True).first
                await option.wait_for(state="visible", timeout=5_000)
                await option.click()

            store_row = panel.locator("div.tw-flex.tw-gap-2.tw-items-center").filter(
                has_text=re.compile(re.escape(STORE_NAME), re.IGNORECASE)
            ).filter(visible=True)

            status = "out_of_stock"
            if await store_row.count():
                row = store_row.first
                green_dot = row.locator('span[class*="#32BE78"]')
                yellow_dot = row.locator('span[class*="#FFA00A"]')
                if await green_dot.count() and await green_dot.first.is_visible():
                    status = "in_stock"
                elif await yellow_dot.count() and await yellow_dot.first.is_visible():
                    status = "few_in_stock"

            if status == "in_stock":
                record["sizes_in_stock"].append(size)
            elif status == "few_in_stock":
                record["sizes_few_in_stock"].append(size)

            # Let the page refresh the selected-size stock indicator before continuing.
            await page.wait_for_timeout(1_000)

        if record["sizes_in_stock"]:
            record["stock"] = "in_stock"
        elif record["sizes_few_in_stock"]:
            record["stock"] = "few_in_stock"

        return record
    finally:
        close_button = panel.locator("button").filter(
            has_text=re.compile(r"^\s*Luk\s*$", re.IGNORECASE)
        ).filter(visible=True)
        if await close_button.count():
            await close_button.first.click()
        else:
            await page.keyboard.press("Escape")


async def read_text(locator):
                               """Return an element's text, or None if it wasn't found."""
                               if await locator.count() == 0:
                                   return None

                               return " ".join((await locator.first.inner_text()).split())


async def get_product_info(page):
                               """Read information shared by this product's color and size variants."""
                               heading = await read_text(page.locator("main h1"))
                               # The category link immediately before the product-name <li>.
                               product_type = await read_text(
                                   page.locator(
                                       "a.tw-opacity-80.tw-font-light.tw-text-nowrap:has(+ li)"
                                   )
                               )

                               # Look for fit only inside the product heading.
                               fit = await read_text(
                                   page.locator("main h1 span.tw-underline")
                               ) or ""

                               product_name = heading
                               if heading and "/" in heading:
                                   product_name = heading.split("/", 1)[1].strip()


                               return {
                                   "price": await read_text(
                                       page.locator('main [x-text="$store.productStore.price"]')
                                   ),
                                   "brand": await read_text(
                                       page.locator('main a[href*="/brands/"]')
                                   ),
                                   "product_name": product_name,
                                   "product_type": product_type or "",
                                   "fit": fit,
                                   "description": await read_text(
                                       page.locator('main a[href="#product-description"]')
                                   ),
                                   "url": page.url,
                               }

async def check_product(page, product):
    """Return one flat record per color, with in-stock sizes grouped by status."""
    # Do not wait for every page resource: a slow asset should not fail the
    # product navigation. Wait for the product heading after the document commits.
    await page.goto(product["url"], wait_until="commit", timeout=30_000)
    await handle_cookie_prompt(page)
    await page.locator("main h1").first.wait_for(state="visible", timeout=20_000)
    product_info = await get_product_info(page)
    product_info["url"] = product["url"]

    colors = await get_product_colors(page)
    if not colors:
        raise RuntimeError("No product color options were found on the page.")

    color_records = []
    for color in colors:
        await select_color(page, color)
        image_urls = await get_color_image_urls(page)
        color_record = await check_color_sizes(page, color, product_info, image_urls)
        color_records.append(color_record)

    return color_records


async def main():
    results = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        page = await browser.new_page()
        page.set_default_timeout(10_000)

        try:
            for product in PRODUCTS:
                try:
                    product_records = await check_product(page, product)
                    results.extend(product_records)
                    print(json.dumps(product_records, ensure_ascii=False, indent=2))
                except Exception as exc:
                    error_record = {
                        "product_name": product["name"],
                        "url": product["url"],
                        "error": str(exc),
                    }
                    results.append(error_record)
                    print(json.dumps(error_record, ensure_ascii=False, indent=2))

            successful_urls = {
                row["url"] for row in results if "error" not in row and row.get("url")
            }
            stocked_urls = {
                row["url"]
                for row in results
                if row.get("stock") in {"in_stock", "few_in_stock"} and row.get("url")
            }
            error_count = sum(1 for row in results if "error" in row)
            print(
                f"\nProducts with at least one available size at {STORE_NAME}: "
                f"{len(stocked_urls)} / {len(successful_urls)}"
            )
            print(f"Products with errors: {error_count}")
        finally:
            await browser.close()

    output_file = Path(__file__).with_name("stock-results.json")
    output_file.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(results)} color record(s) to {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
