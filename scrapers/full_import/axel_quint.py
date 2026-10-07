"""Public Shopware/Alpine catalogue data shared by AXEL and qUINT.

No changes to Kaufmann: the stores share a source format, not store identity.
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger(__name__)
# Current product scope is Aarhus only. Other source locations are debug data.
CONFIG = {
    "axel": {"host": "www.axel.dk", "aarhus": "aarhus", "stores": ("aarhus",)},
    "quint": {"host": "www.quint.dk", "aarhus": "bruuns-galleri", "stores": ("bruuns-galleri",)},
}

# Only product-specific sections are read; recommendation cards and brand copy
# elsewhere on the page must never supply descriptions or prices.
SNAPSHOT_JS = """async ({full}) => {
    const store = Alpine.store('productStore');
    const trackingNode = document.querySelector('[data-relewise-tracking-options]');
    const tracking = JSON.parse(trackingNode?.getAttribute('data-relewise-tracking-options') || '{}');
    const result = {tracking, url: location.href, canonical:
        document.querySelector('link[rel="canonical"]')?.href || location.href,
        size_guide_html: full ? (document.querySelector('.sizeGuideSizeContent')?.innerHTML || '') : '',
        jsonld: [...document.querySelectorAll('script[type="application/ld+json"]')].flatMap(el => {
            try {return [JSON.parse(el.textContent)];} catch {return [];}
        }), colors: {}};
    for (const [id, option] of Object.entries(store.options || {})) {
        if (full) {
            store.setColor(id);
            await Alpine.nextTick();
            await new Promise(resolve => setTimeout(resolve, 100));
            if (store.colorId !== id) throw new Error('Colour selection did not settle');
        }
        result.colors[id] = {option: JSON.parse(JSON.stringify(option))};
        if (full) {
            result.colors[id].images = JSON.parse(JSON.stringify(store.images || []));
            result.colors[id].description_html = document.querySelector('#product-description .product-rich-text')?.innerHTML || '';
            result.colors[id].specifications = [...document.querySelectorAll('#product-description > div > ul > li')]
                .map(li => li.textContent.replace(/\\s+/g, ' ').trim());
        }
    }
    return result;
}"""


class SourceUnavailable(requests.HTTPError):
    """Only an explicit HTTP 404/410 is a missing product."""


class IncompleteProduct(ValueError):
    """Malformed identity, currency or inventory must not overwrite live rows."""


def text(value: Any) -> str:
    return " ".join(str(value or "").split())


def quantity(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or int(value) != value:
        raise IncompleteProduct(f"Invalid stock quantity: {value!r}")
    return int(value)


def price(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise IncompleteProduct(f"Invalid numeric price: {value!r}")
    return float(value)


def formatted_price(value: Any) -> float | None:
    value = text(value)
    if not value:
        return None
    match = re.fullmatch(r"(?:DKK\s*)?([0-9]+(?:\.[0-9]{3})*(?:,[0-9]{1,2})?)(?:\s*kr\.?)?", value)
    if not match:
        raise IncompleteProduct(f"Unrecognized DKK price: {value!r}")
    return price(float(match[1].replace('.', '').replace(',', '.')))


def parse_size_guide(html: str) -> dict:
    soup = BeautifulSoup(html, 'html.parser')
    tables = []
    for table in soup.select('table'):
        rows = [[text(cell.get_text(' ', strip=True)) for cell in row.select('th, td')] for row in table.select('tr')]
        rows = [row for row in rows if any(row)]
        if rows:
            tables.append(rows)
    content = text(soup.get_text(' ', strip=True))
    return {'text': content, 'tables': tables} if content else {}


def jsonld_items(items):
    for item in items:
        if isinstance(item, list):
            yield from jsonld_items(item)
        elif isinstance(item, dict):
            yield item
            yield from jsonld_items(item.get('@graph', []))


def taxonomy(snapshot: dict) -> tuple[str | None, list[str], list[str]]:
    breadcrumbs = next((x for x in jsonld_items(snapshot.get('jsonld', [])) if x.get('@type') == 'BreadcrumbList'), {})
    source = [text(x.get('name')) for x in breadcrumbs.get('itemListElement', []) if x.get('item')]
    labels = [x.casefold() for x in source]
    # Exclude accessories even if nested underneath the clothing department.
    if any(any(word in label for word in ('accessor', 'bælte', 'kasket', 'hue', 'taske', 'halstør', 'gavekort', 'pleje')) for label in labels):
        return None, [], source
    if any('sko' in x or 'sneaker' in x or 'støvle' in x for x in labels):
        return 'Footwear', ['Footwear', 'Shoes'], source
    if 'tøj' not in labels:
        return None, [], source
    mapping = (('jeans', 'Jeans'), ('bukser', 'Pants'), ('shorts', 'Shorts'), ('skjorter', 'Shirts'),
               ('t-shirts', 'T-shirts'), ('polo', 'Polos'), ('strik', 'Knitwear'), ('sweat', 'Sweatshirts'),
               ('jakker', 'Jackets'), ('frakker', 'Coats'), ('blazer', 'Blazers'), ('jakkesæt', 'Suits'),
               ('undertøj', 'Underwear'), ('strømper', 'Socks'), ('veste', 'Vests'))
    leaf = next((category for label in reversed(labels) for token, category in mapping if token in label), 'Clothing')
    return leaf, ['Clothing'] + ([leaf] if leaf != 'Clothing' else []), source


class AxelQuintScraper:
    def __init__(self, store: str, *, max_retries: int = 2):
        self.store = store
        self.config = CONFIG[store]
        self.base_url = 'https://' + self.config['host']
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; HIGHLIDE catalogue/1.0)', 'Accept-Language': 'da-DK,da;q=0.9'})
        retry = Retry(total=max_retries, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504), allowed_methods={'GET'})
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self._playwright = self._browser = self._page = None

    def clean_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != 'https' or parts.netloc != self.config['host'] or not parts.path.startswith('/produkt/'):
            raise ValueError(f'Not a {self.store} product URL: {url}')
        return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))

    def discover_product_urls(self) -> list[str]:
        """Follow every sitemap child; never turn failed discovery into an empty catalogue."""
        pending = [self.base_url + '/sitemap.xml']
        visited, products = set(), set()
        while pending:
            url = pending.pop(0)
            if url in visited:
                continue
            if urlsplit(url).netloc != self.config['host']:
                raise ValueError('Cross-store sitemap URL')
            visited.add(url)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            data = response.content
            if data.startswith(b'\x1f\x8b'):
                data = gzip.decompress(data)
            root = ET.fromstring(data)
            locations = [x.text.strip() for x in root.findall('.//{*}loc') if x.text]
            if root.tag.rsplit('}', 1)[-1] == 'sitemapindex':
                pending.extend(locations)
            elif root.tag.rsplit('}', 1)[-1] == 'urlset':
                for location in locations:
                    if urlsplit(location).path.startswith('/produkt/'):
                        products.add(self.clean_url(location))
            else:
                raise ValueError('Unexpected sitemap format')
            if len(visited) > 100:
                raise ValueError('Sitemap traversal limit exceeded')
        if not products:
            raise IncompleteProduct('Sitemap contained no product URLs')
        return sorted(products)

    def fetch_snapshot(self, url: str, *, full: bool = True) -> dict:
        from playwright.sync_api import sync_playwright
        url = self.clean_url(url)
        if self._browser is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
            self._page = self._browser.new_page(locale='da-DK')
        for attempt in range(self.max_retries + 1):
            try:
                response = self._page.goto(url, wait_until='domcontentloaded', timeout=45000)
                if response is None:
                    raise IncompleteProduct('Navigation returned no HTTP response')
                if response.status in (404, 410):
                    missing = requests.Response()
                    missing.status_code = response.status
                    missing.url = url
                    raise SourceUnavailable(f'HTTP {response.status} for {url}', response=missing)
                if response.status >= 400:
                    raise IncompleteProduct(f'HTTP {response.status} for {url}')
                self.clean_url(self._page.url)
                self._page.wait_for_function("window.Alpine && Object.keys(Alpine.store('productStore')?.options || {}).length > 0", timeout=20000)
                snapshot = self._page.evaluate(SNAPSHOT_JS, {'full': full})
                snapshot['canonical'] = self.clean_url(snapshot['canonical'])
                return snapshot
            except SourceUnavailable:
                raise
            except Exception:
                if attempt == self.max_retries:
                    raise
                time.sleep(2 ** attempt)
        raise AssertionError('unreachable')

    def rows_from_snapshot(self, snapshot: dict, *, full: bool = True) -> list[dict]:
        parent = text(snapshot.get('tracking', {}).get('parentId'))
        colors = snapshot.get('colors')
        if not parent or not isinstance(colors, dict) or not colors:
            raise IncompleteProduct('Missing parent identity or colour options')
        canonical = self.clean_url(snapshot['canonical'])
        product = next((x for x in jsonld_items(snapshot.get('jsonld', [])) if x.get('@type') == 'Product'), {})
        offers = product.get('offers', {})
        if not isinstance(offers, dict) or offers.get('priceCurrency') != 'DKK':
            raise IncompleteProduct('Product currency is not verified DKK')
        category, category_path, source_path = taxonomy(snapshot)
        if full and not category:
            if not source_path:
                raise IncompleteProduct('No product taxonomy; cannot safely classify catalogue item')
            return []
        checked = datetime.now(timezone.utc).isoformat()
        rows = []
        for color_id, payload in colors.items():
            option = payload['option']
            if not isinstance(option.get('available'), bool):
                raise IncompleteProduct('Missing explicit colour availability')
            available = option['available']
            sizes = option.get('sizes')
            if not color_id or not isinstance(sizes, dict) or not sizes:
                raise IncompleteProduct('Empty colour identity or sizes')
            current = price(option.get('priceRaw'))
            former = option.get('listPriceRaw')
            former = price(former) if former is not None else formatted_price(option.get('listPrice'))
            if former is not None and former <= current:
                former = None
            stores, webshop, raw_stock, seen_labels = {}, [], {}, set()
            for variant_id, size in sizes.items():
                label = text(size.get('sizeName'))
                if not label or label in seen_labels or str(size.get('productId')) != variant_id:
                    raise IncompleteProduct('Missing/duplicate size label or mismatched variant identity')
                seen_labels.add(label)
                source_stock = size.get('stock')
                if not isinstance(source_stock, dict):
                    raise IncompleteProduct('Missing per-store stock payload')
                by_slug = {entry.get('seoUrl'): entry for entry in source_stock.values()}
                raw_stock[variant_id] = source_stock
                online = quantity(size.get('availability'))
                webshop.append({'size': label, 'available': online > 0, 'stock': None,
                                'source_availability': online,
                                'online_warehouse_stock': quantity(by_slug['online-shop']['stock']) if 'online-shop' in by_slug else None,
                                'source_size_variant_id': variant_id, 'source_product_number': size.get('productNumber')})
                for slug in self.config['stores']:
                    entry = by_slug.get(slug)
                    if entry is None:
                        raise IncompleteProduct(f'Missing {slug} inventory for {label}; preserving previous state')
                    count = quantity(entry.get('stock'))
                    key = f'{self.store}-{slug}'
                    if key not in stores:
                        stores[key] = {'name': text(entry.get('label')), 'stock_known': True, 'available': False,
                                       'total_stock': 0, 'sizes': {}, 'address': text(' '.join(str(entry.get(k) or '') for k in ('street', 'streetNumber', 'zipCode', 'city')))}
                    stores[key]['sizes'][label] = {'available': count > 0, 'stock': count}
                    stores[key]['total_stock'] += count
                    stores[key]['available'] = stores[key]['total_stock'] > 0
            # Like Kaufmann, the explicit discontinued flag overrides stale stock.
            # Keep the untouched source option/inventory in raw for diagnostics.
            if not available:
                for store in stores.values():
                    store['available'] = False
                    store['total_stock'] = 0
                    for size in store['sizes'].values():
                        size.update(available=False, stock=0)
                for size in webshop:
                    size.update(available=False, stock=0, online_warehouse_stock=0)
            total = sum(x['total_stock'] for x in stores.values())
            aarhus = stores[f'{self.store}-{self.config["aarhus"]}']
            row = {'source_parent_id': parent, 'source_color_id': color_id,
                   'current_price': current, 'list_price': former, 'webshop_sizes': webshop,
                   'local_inventory': {'stores': stores}, 'local_total_stock': total,
                   'local_available': total > 0, 'aarhus_total_stock': aarhus['total_stock'],
                   'aarhus_available': aarhus['available'], 'inventory_checked_at': checked,
                   'scraped_at': checked, 'updated_at': checked,
                   'publication_status': 'active' if available else 'unavailable',
                   'status_reason': None if available else 'source_available_false',
                   'status_checked_at': checked, 'discontinued_at': None if available else checked}
            if full:
                specifications = {}
                for line in payload.get('specifications', []):
                    key, sep, value = line.partition(':')
                    if sep:
                        specifications[text(key)] = text(value)
                description_soup = BeautifulSoup(payload.get('description_html', ''), 'html.parser')
                description = description_soup.get_text(' ', strip=True) or product.get('description') or None
                images = list(dict.fromkeys(image.get('full_src') or image.get('src') for image in payload.get('images', []) if image.get('full_src') or image.get('src')))
                name = text(product.get('name')) or specifications.get('Modelnavn')
                if not name or not images:
                    raise IncompleteProduct('Missing product name or colour images')
                row.update({'source_url': f'{canonical}?color={color_id}#color={color_id}', 'canonical_url': canonical,
                            'source_product_number': specifications.get('Varenummer'),
                            'name': name, 'brand': option.get('manufacturer'), 'product_type': source_path[-1] if source_path else None,
                            'color': option.get('name'), 'color_group': option.get('colorGroup'), 'currency': 'DKK',
                            'description': description, 'specifications': specifications,
                            'materials': [text(x) for x in specifications.get('Materiale', '').split(',') if text(x)],
                            'fit': specifications.get('Fit'), 'category': category, 'category_path': category_path,
                            'images': images, 'size_guide': parse_size_guide(snapshot.get('size_guide_html', '')), 'raw': {'tracking': snapshot['tracking'], 'source_category_path': source_path,
                            'source_inventory': raw_stock, 'source_option': option, 'jsonld_product': product,
                            'source_available': option.get('available'), 'description_html': payload.get('description_html', '')}})
            rows.append(row)
        return rows

    def scrape(self, url: str, *, full: bool = True) -> list[dict]:
        return self.rows_from_snapshot(self.fetch_snapshot(url, full=full), full=full)

    def close(self):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self.session.close()
            self._browser = self._playwright = self._page = None
