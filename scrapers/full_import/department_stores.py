"""Shared public HTTP transport and row helpers for department-store imports."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scrapers.full_import.axel_quint import SourceUnavailable


def text(value):
    return re.sub(r'\s+', ' ', BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)).strip()


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f'Invalid price/quantity: {value!r}')
    return value


def integer(value):
    result = number(value)
    if int(result) != result:
        raise ValueError('Fractional stock quantity')
    return int(result)


def inventory_row(store, name, sizes, known):
    if not sizes:
        raise ValueError('No validated size inventory')
    total = sum(v['stock'] for v in sizes.values()) if known else None
    available = (True if any(v['available'] is True for v in sizes.values()) else
                 None if any(v['available'] is None for v in sizes.values()) else False)
    return dict(local_inventory={'stores': {f'{store}-aarhus': {
        'name': name, 'stock_known': known, 'available': available,
        'total_stock': total, 'sizes': sizes,
    }}}, local_total_stock=total, aarhus_total_stock=total,
        local_available=available, aarhus_available=available)


def timestamps():
    now = datetime.now(timezone.utc).isoformat()
    return dict(inventory_checked_at=now, scraped_at=now, updated_at=now)


def classification(path, name=''):
    """Conservative men's clothing/footwear gate; retain the source category path."""
    labels = [text(v).casefold() for v in path]
    if not labels or labels[0] != 'herre':
        return None
    if not any(v in ('tøj', 'sko') for v in labels):
        return None
    if any(term in ' '.join(labels) for term in ('accessor', 'slips', 'butterfly', 'lommeklude', 'manchetknapper', 'indlægssåler')):
        return None
    joined = ' '.join(labels + [text(name).casefold()])
    if any(term in joined for term in ('gavekort', 'skopleje', 'skohorn', 'snørebånd', 'imprægner', 'dressing voks', 'tilbehør', 'accessories', 'bælter', 'kasketter', 'huer', 'halstørklæder', 'tasker')):
        return None
    return 'Footwear' if 'sko' in labels else 'Clothing'


def taxonomy(path):
    if 'sko' in [v.casefold() for v in path]:
        return 'Footwear', ['Footwear', 'Shoes']
    mapping = (('jeans','Jeans'), ('bukser','Pants'), ('shorts','Shorts'), ('skjorter','Shirts'),
               ('t-shirts','T-shirts'), ('polo','Polos'), ('strik','Knitwear'), ('sweat','Sweatshirts'),
               ('jakker','Jackets'), ('frakker','Coats'), ('blazer','Blazers'), ('jakkesæt','Suits'),
               ('undertøj','Underwear'), ('strømper','Socks'), ('veste','Vests'))
    leaf = next((value for label in reversed(path) for key,value in mapping if key in label.casefold()), 'Clothing')
    return leaf, ['Clothing'] + ([leaf] if leaf != 'Clothing' else [])


class PublicScraper:
    BASE_URL = ''

    def __init__(self, *, max_retries=2, request_delay=.4):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': 'Mozilla/5.0 (compatible; HIGHLIDE catalog)',
                                     'Accept-Language': 'da-DK,da;q=0.9', 'Cache-Control': 'no-cache'})
        retry = Retry(total=max_retries, backoff_factor=1, status_forcelist=(429,500,502,503,504),
                      allowed_methods=frozenset({'GET'}), raise_on_status=False)
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.request_delay = request_delay
        self.last_request = 0
        self.covered_urls = set()

    def clean_url(self, url):
        parsed = urlsplit(urljoin(self.BASE_URL, url))
        if parsed.scheme != 'https' or parsed.netloc != urlsplit(self.BASE_URL).netloc:
            raise ValueError(f'Unexpected source host: {parsed.netloc}')
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))

    def get(self, url):
        parsed = urlsplit(urljoin(self.BASE_URL, url))
        if parsed.scheme != 'https' or parsed.netloc != urlsplit(self.BASE_URL).netloc:
            raise ValueError('Unexpected source endpoint')
        time.sleep(max(0, self.request_delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.session.get(parsed.geturl(), timeout=60)
        if response.status_code in (404,410):
            raise SourceUnavailable(f'Source unavailable: {parsed.path}', response=response)
        response.raise_for_status()
        if urlsplit(response.url).netloc != parsed.netloc:
            raise ValueError('Unexpected cross-host redirect')
        return response

    def scrape(self, url, full=True):
        return self.rows_from_snapshot(self.fetch_snapshot(url, full=full), full=full)

    def close(self):
        self.session.close()


def make_scraper(store, **kwargs):
    from scrapers.full_import.salling import SallingScraper
    from scrapers.full_import.magasin import MagasinScraper
    return {'salling': SallingScraper, 'magasin': MagasinScraper}[store](**kwargs)
