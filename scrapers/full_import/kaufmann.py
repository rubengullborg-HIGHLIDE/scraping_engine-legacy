import gzip
import json
import logging
import random
import re
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LOG = logging.getLogger(__name__)

AARHUS_STORES = (
    {"seo_url": "bruuns-galleri", "label": "KAUFMANN Aarhus, Bruuns Galleri"},
    {"seo_url": "storcenter-nord", "label": "KAUFMANN Aarhus, Storcenter Nord"},
    {"seo_url": "aarhus-c", "label": "KAUFMANN Aarhus, Strøget - Regina"},
)


class BaseScraper(ABC):
    def __init__(self):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

    def fetch_html(self, url):
        try:
            res = requests.get(url, headers=self.headers, timeout=10)
            res.raise_for_status()
            return res.text
        except Exception as e:
            print(f"  [!] Fejl ved {url}: {e}")
            return None

    @abstractmethod
    def get_product_links(self, html):
        pass

    @abstractmethod
    def parse_product_details(self, html):
        pass


class KaufmanScraper(BaseScraper):
    def __init__(self, launch_browser: bool = True):
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "da-DK,da;q=0.9",
        }
        self._playwright = None
        self._browser = None
        if launch_browser:
            self._ensure_browser()

    def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)

    def fetch_html_with_js(self, url):
        self._ensure_browser()
        assert self._browser is not None
        page = self._browser.new_page()
        page.goto(url, wait_until="networkidle")
        try:
            page.click('button:has-text("Vælg")', timeout=3000)
            page.wait_for_selector("ul.tw-font-medium.tw-text-base li", timeout=3000)
        except:
            pass
        html = page.content()
        page.close()
        return html

    def discover_product_links_from_sitemap(
        self,
        sitemap_url: str = "https://www.kaufmann.dk/sitemap.xml",
        max_sitemaps: int = 100,
    ) -> list[str]:
        pending = [sitemap_url]
        seen_sitemaps: set[str] = set()
        product_links: set[str] = set()

        while pending and len(seen_sitemaps) < max_sitemaps:
            current = pending.pop(0)
            if current in seen_sitemaps:
                continue
            seen_sitemaps.add(current)

            xml = self._fetch_sitemap_xml(current)
            if not xml:
                continue

            try:
                root = ET.fromstring(xml)
            except ET.ParseError:
                continue

            for loc in root.iter():
                if not loc.tag.endswith("loc") or not loc.text:
                    continue
                url = loc.text.strip()
                if "/produkt/" in url:
                    product_links.add(self._clean_product_url(url))
                elif "sitemap" in url and (url.endswith(".xml") or url.endswith(".xml.gz")):
                    pending.append(url)

        return sorted(product_links)

    def _fetch_sitemap_xml(self, url: str) -> Optional[str]:
        try:
            response = requests.get(
                url,
                headers=self.headers,
                timeout=30,
            )
            response.raise_for_status()

            data = response.content

            # The server returned plain XML.
            if data.lstrip().startswith(b"<"):
                return data.decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )

            # Genuine gzip data starts with 1f 8b.
            if data[:2] == b"\x1f\x8b":
                return gzip.decompress(data).decode(
                    "utf-8",
                    errors="replace",
                )

            return data.decode(
                response.encoding or "utf-8",
                errors="replace",
            )

        except requests.RequestException as exc:
            LOG.error(
                "Kaufmann sitemap request failed for %s: %s",
                url,
                exc,
            )
            return None

        except (
            OSError,
            gzip.BadGzipFile,
            UnicodeDecodeError,
        ) as exc:
            LOG.error(
                "Kaufmann sitemap decoding failed for %s: %s",
                url,
                exc,
            )
            return None

    def parse_product_variants_with_js(
        self,
        url: str,
        allow_unavailable: bool = False,
    ) -> list[dict[str, Any]]:
        self._ensure_browser()
        assert self._browser is not None
        page = self._browser.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if allow_unavailable and response and response.status in {404, 410}:
                return []
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass

            try:
                page.wait_for_function(
                    "window.Alpine && Alpine.store && Alpine.store('productStore') "
                    "&& Object.keys(Alpine.store('productStore').options || {}).length > 0",
                    timeout=20000,
                )
            except Exception:
                if allow_unavailable and self._looks_like_unavailable_page(page):
                    return []
                raise

            variants = page.evaluate(
                """
                async ({ aarhusStores }) => {
                  const store = Alpine.store('productStore');
                  const aarhusSeoUrls = aarhusStores.map((store) => store.seo_url);
                  const canonical = document.querySelector('link[rel="canonical"]')?.href
                    || document.querySelector('meta[property="og:url"]')?.content
                    || location.href.split('?')[0];
                  const trackingRaw = document
                    .querySelector('[data-relewise-tracking-options]')
                    ?.getAttribute('data-relewise-tracking-options');
                  let tracking = {};
                  try { tracking = trackingRaw ? JSON.parse(trackingRaw) : {}; } catch (_) {}

                  const titleParts = (document.title || '')
                    .split('|')
                    .map((part) => part.trim())
                    .filter(Boolean);
                  const jsonLdProducts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                    .map((script) => {
                      try { return JSON.parse(script.textContent || '{}'); } catch (_) { return null; }
                    })
                    .filter((item) => item && (item['@type'] === 'Product' || item.type === 'Product'));
                  const jsonLdProduct = jsonLdProducts[0] || {};
                  const productName = titleParts[0] || null;
                  const brand = titleParts[2]
                    || document.querySelector('[itemprop="brand"]')?.textContent?.trim()
                    || jsonLdProduct.brand?.name
                    || null;
                  const description = document.querySelector('[itemprop="description"]')?.textContent?.replace(/\\s+/g, ' ').trim()
                    || jsonLdProduct.description
                    || null;

                  function parsePrice(value) {
                    if (typeof value === 'number') return value;
                    if (!value) return null;
                    const match = String(value).replace(/\\./g, '').replace(',', '.').match(/\\d+(?:\\.\\d+)?/);
                    return match ? Number(match[0]) : null;
                  }

                  function specValue(label) {
                    const items = Array.from(document.querySelectorAll('#product-description li'));
                    const item = items.find((li) => li.textContent.includes(label));
                    return item ? item.textContent.replace(label, '').trim() : null;
                  }

                  const materialsText = specValue('Materiale:');
                  const materials = materialsText
                    ? materialsText.split(',').map((part) => part.trim()).filter(Boolean)
                    : [];
                  const fit = specValue('Fit:');

                  function variantUrl(colorId) {
                    return `${canonical}?color=${colorId}#color=${colorId}`;
                  }

                  function summarizeSizes() {
                    return Object.values(store.currentOptions || {}).map((option) => {
                      const allStores = Object.values(option.stock || {});
                      const aarhusStores = allStores
                        .filter((stock) => aarhusSeoUrls.includes(stock.seoUrl))
                        .map((stock) => ({
                          label: stock.label || null,
                          seo_url: stock.seoUrl || null,
                          warehouse_id: stock.warehouseId || null,
                          available: Boolean(stock.available),
                          stock: Number(stock.stock || 0),
                        }));
                      const aarhusTotalStock = aarhusStores.reduce((sum, stock) => sum + Number(stock.stock || 0), 0);

                      return {
                        size: String(option.sizeName || '').trim(),
                        in_stock: aarhusTotalStock > 0,
                        aarhus_total_stock: aarhusTotalStock,
                        webshop_stock: Number(option.availability || 0),
                        source_size_variant_id: option.productId || null,
                        source_product_number: option.productNumber || null,
                        stores: aarhusStores,
                      };
                    });
                  }

                  function summarizeAarhusInventory(sizes) {
                    const summary = {
                      stores: Object.fromEntries(
                        aarhusStores.map((store) => [
                          store.seo_url,
                          {
                            name: store.label || null,
                            stock_known: true,
                            available: false,
                            total_stock: 0,
                            sizes: {},
                          },
                        ])
                      ),
                    };

                    for (const size of sizes) {
                      for (const storeInfo of aarhusStores) {
                        const stock = (size.stores || []).find((item) => item.seo_url === storeInfo.seo_url);
                        const storeSummary = summary.stores[storeInfo.seo_url];
                        const stockCount = Number(stock?.stock || 0);
                        const available = stockCount > 0 || Boolean(stock?.available);
                        storeSummary.total_stock += stockCount;
                        storeSummary.available = storeSummary.available || available;
                        storeSummary.sizes[size.size] = {
                          available,
                          stock: stockCount,
                        };
                      }
                    }
                    return summary;
                  }

                  function summarizeSourceAarhusInventory(sizes) {
                    const summary = {
                      stores: Object.fromEntries(
                        aarhusStores.map((store) => [
                          store.seo_url,
                          {
                            warehouse_id: null,
                            sizes: {},
                          },
                        ])
                      ),
                    };

                    for (const size of sizes) {
                      for (const storeInfo of aarhusStores) {
                        const stock = (size.stores || []).find((item) => item.seo_url === storeInfo.seo_url);
                        const storeSummary = summary.stores[storeInfo.seo_url];
                        if (stock?.warehouse_id) storeSummary.warehouse_id = stock.warehouse_id;
                        storeSummary.sizes[size.size] = {
                          source_size_variant_id: size.source_size_variant_id,
                          source_product_number: size.source_product_number,
                        };
                      }
                    }
                    return summary;
                  }

                  const results = [];
                  for (const [colorId, option] of Object.entries(store.options || {})) {
                    if (typeof store.setColor === 'function') {
                      store.setColor(colorId);
                      await new Promise((resolve) => setTimeout(resolve, 350));
                    }

                    const sourceAvailable = Boolean(store.available);
                    const sizes = summarizeSizes();
                    let aarhusTotalStock = sizes.reduce((sum, size) => sum + Number(size.aarhus_total_stock || 0), 0);
                    const aarhusInventory = summarizeAarhusInventory(sizes);
                    if (!sourceAvailable) {
                      aarhusTotalStock = 0;
                      for (const storeSummary of Object.values(aarhusInventory.stores || {})) {
                        storeSummary.available = false;
                        storeSummary.total_stock = 0;
                        for (const sizeSummary of Object.values(storeSummary.sizes || {})) {
                          sizeSummary.available = false;
                          sizeSummary.stock = 0;
                        }
                      }
                    }
                    const images = (store.images || [])
                      .map((image) => image.full_src || image.src || image.thumb_big_src || image.thumb_src)
                      .filter(Boolean);

                    results.push({
                      source_parent_id: tracking.parentId || null,
                      source_color_id: colorId,
                      source_url: variantUrl(colorId),
                      canonical_url: canonical,
                      source_product_number: null,
                      name: productName,
                      brand,
                      color: store.colorName || option.name || null,
                      color_group: store.colorGroup || option.colorGroup || null,
                      current_price: parsePrice(store.priceRaw ?? store.price ?? option.priceRaw ?? option.price),
                      list_price: parsePrice(store.listPriceRaw ?? store.listPrice ?? option.listPriceRaw ?? option.listPrice),
                      currency: 'DKK',
                      description,
                      materials,
                      fit,
                      category: null,
                      images,
                      webshop_sizes: sizes.map((size) => ({
                        size: size.size,
                        in_stock: sourceAvailable && Number(size.webshop_stock || 0) > 0,
                        stock: sourceAvailable ? size.webshop_stock : 0,
                        source_size_variant_id: size.source_size_variant_id,
                        source_product_number: size.source_product_number,
                      })),
                      aarhus_inventory: aarhusInventory,
                      aarhus_total_stock: aarhusTotalStock,
                      aarhus_available: sourceAvailable && aarhusTotalStock > 0,
                      source_available: sourceAvailable,
                      publication_status: sourceAvailable ? 'active' : 'unavailable',
                      status_reason: sourceAvailable ? null : 'source_available_false',
                      raw: {
                        tracking,
                        aarhus_store_seo_urls: aarhusSeoUrls,
                        source_aarhus_inventory: summarizeSourceAarhusInventory(sizes),
                        scraped_from_url: location.href,
                      },
                    });
                  }
                  return results;
                }
                """,
                {"aarhusStores": list(AARHUS_STORES)},
            )

            scraped_at = datetime.now(timezone.utc).isoformat()
            cleaned = []
            for variant in variants:
                if not variant.get("source_parent_id"):
                    variant["source_parent_id"] = self._source_parent_id_from_url(url)
                variant["status_checked_at"] = scraped_at
                variant["discontinued_at"] = (
                    None if variant.get("source_available") else scraped_at
                )
                variant["last_inventory_checked_at"] = scraped_at
                variant["last_seen_in_catalog_at"] = scraped_at
                variant["consecutive_source_misses"] = 0
                variant["consecutive_catalog_misses"] = 0
                variant["last_refresh_error"] = None
                variant["last_refresh_error_at"] = None
                variant["scraped_at"] = scraped_at
                variant["updated_at"] = scraped_at
                cleaned.append(variant)
            return cleaned
        finally:
            page.close()

    def close(self):
        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def _looks_like_unavailable_page(self, page: Any) -> bool:
        text = page.locator("body").inner_text(timeout=3000).lower()
        unavailable_patterns = (
            "siden blev ikke fundet",
            "produktet blev ikke fundet",
            "produktet findes ikke",
            "404",
        )
        return any(pattern in text for pattern in unavailable_patterns)

    def _clean_product_url(self, url: str) -> str:
        split = urlsplit(url.split("#", 1)[0])
        return urlunsplit((split.scheme, split.netloc, split.path, "", ""))

    def _source_parent_id_from_url(self, url: str) -> str:
        return self._clean_product_url(url).rstrip("/").rsplit("/", 1)[-1]

    def _get_product_base_id(self, soup: BeautifulSoup) -> Optional[str]:
        og = soup.select_one('meta[property="og:url"]')
        canonical = soup.select_one('link[rel="canonical"]')

        url = None
        if og and og.get("content"):
            url = og["content"]
        elif canonical and canonical.get("href"):
            url = canonical["href"]

        if not url:
            return None

        m = re.search(r"-(\d+)(?:$|\?)", url)
        if not m:
            return None

        digits = m.group(1)
        if len(digits) >= 6:
            return digits[:6]
        return digits

    def _extract_images_from_raw_html(
        self, html: str, base: Optional[str] = None
    ) -> list[str]:

        if not html:
            return []

        if not base:
            base_match = re.search(
                r"/media/[^\"'\s>]+/(\d{5,8})_[A-Za-z0-9_-]+\.(?:png|jpe?g)",
                html,
                flags=re.IGNORECASE,
            )
            if not base_match:
                return []
            base = base_match.group(1)

        url_pattern = rf"https:(?:\\/\\/|//)www\.kaufmann\.dk(?:\\/|/)cdn-cgi(?:\\/|/)image(?:\\/|/)[^\"'\s>]+?(?:\\/|/)media(?:\\/|/)[^\"'\s>]+?(?:\\/|/){base}_[A-Za-z0-9_-]+\.(?:png|jpe?g)"
        urls = re.findall(url_pattern, html, flags=re.IGNORECASE)
        if not urls:
            return []

        preferred_prefix = "https://www.kaufmann.dk/cdn-cgi/image/width%3D1472%2Cheight%3D1472%2Cformat%3Dauto"

        by_filename: dict[str, str] = {}
        for u in urls:
            u = u.replace("\\/", "/")
            try:
                tail = u[u.index("/media/") :]
            except ValueError:
                continue

            filename = tail.rsplit("/", 1)[-1]
            by_filename.setdefault(filename, preferred_prefix + tail)

        def sort_key(item: tuple[str, str]):
            filename, _ = item
            m = re.search(r"-(\d{2})\.(?:png|jpe?g)$", filename, flags=re.IGNORECASE)
            return (0, int(m.group(1))) if m else (1, filename.lower())

        return [u for _, u in sorted(by_filename.items(), key=sort_key)]

    def get_product_links(self, html):
        soup = BeautifulSoup(html, "html.parser")
        links = []

        for a in soup.find_all("a", href=True):
            href = a["href"]

            if "/produkt/" not in href:
                continue

            clean_url = href.split("#")[0]

            if clean_url.startswith("/"):
                clean_url = f"https://www.kaufmann.dk{clean_url}"

            links.append(clean_url)

        return list(set(links))

    def parse_product_details(self, html):
        soup = BeautifulSoup(html, "html.parser")

        title_text = soup.title.get_text(strip=True) if soup.title else ""
        parts = [p.strip() for p in title_text.split("|")]

        produkt_navn = parts[0] if len(parts) >= 1 else ""
        farve = parts[1] if len(parts) >= 2 else ""
        brand = parts[2] if len(parts) >= 3 else "Ukendt"

        price_tag = soup.select_one('div[x-text="$store.productStore.price"]')
        if price_tag:
            price_text = price_tag.get_text(strip=True)  # fx "DKK 800"
            price_numbers = re.sub(r"[^\d]", "", price_text)
            pris = float(price_numbers) if price_numbers else 0.0
        else:
            raw_price = parts[3] if len(parts) >= 4 else ""
            price_numbers = re.sub(r"[^\d]", "", raw_price)
            pris = float(price_numbers) if price_numbers else 0.0

        desc_div = soup.select_one('div[itemprop="description"]')
        description = desc_div.get_text(" ", strip=True) if desc_div else ""

        page_text = str(soup)

        in_stock = re.findall(
            r"selectSize\([^)]+\)[^>]*>.*?<span[^>]*tw-leading-6\.5[^>]*>([^<]+)</span>",
            page_text,
            re.DOTALL,
        )

        out_of_stock = re.findall(
            r"<div[^>]*tw-flex[^>]*>\s*<span[^>]*tw-line-through[^>]*>([^<]+)</span>.*?remindMe",
            page_text,
            re.DOTALL,
        )

        in_stock_names = {s.strip() for s in in_stock}
        size_status = []

        for size in in_stock:
            size = size.strip()
            if size:
                size_status.append({"size": size, "in_stock": True})

        for size in out_of_stock:
            size = size.strip()
            if size and size not in in_stock_names:
                size_status.append({"size": size, "in_stock": False})

        # Deduplicate preserving order
        seen = set()
        unique_sizes = []
        for s in size_status:
            if s["size"] not in seen:
                seen.add(s["size"])
                unique_sizes.append(s)
        size_status = unique_sizes

        idx = page_text.find("selectSize")
        print("RAW HTML around selectSize:")
        print(repr(page_text[idx - 100 : idx + 300]))

        base_id = self._get_product_base_id(soup)
        images = self._extract_images_from_raw_html(html, base=base_id)

        if not images:
            thumb_imgs = soup.select(
                'div.tw-flex.tw-flex-col.tw-gap-2.tw-items-center img[sizes="80px"]'
            )

            for img in thumb_imgs:
                url = img.get("src")
                if url and not url.startswith("data:image") and url not in images:
                    images.append(url)

        if not images:
            gallery_imgs = soup.select(
                "#image-gallery-desktop img, #image-gallery-mobile img"
            )
            for img in gallery_imgs:
                srcset = img.get("srcset")
                if srcset:
                    url = srcset.split(",")[-1].strip().split(" ")[0]
                else:
                    url = img.get("src")

                if url and not url.startswith("data:image") and url not in images:
                    images.append(url)

        materials = []
        color = farve or "Ukendt"
        fit = "Ukendt"

        specs_list = soup.select("#product-description ul li")
        for li in specs_list:
            text = li.get_text(" ", strip=True)

            if "Farve:" in text and not farve:
                color = text.replace("Farve:", "").strip()
            elif "Fit:" in text:
                fit = text.replace("Fit:", "").strip()
            elif "Materiale:" in text:
                m_text = text.replace("Materiale:", "").strip()
                materials.extend([m.strip() for m in m_text.split(",")])

        return {
            "navn": produkt_navn,
            "pris": pris,
            "brand": brand,
            "description": description,
            "materials": materials,
            "color": color,
            "fit": fit,
            "sizes": size_status,
            "images": images,
            "store": "Kaufmann Aarhus",
        }
