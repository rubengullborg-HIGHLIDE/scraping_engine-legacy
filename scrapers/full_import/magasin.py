"""Magasin public variation and size-level stock endpoints; Aarhus only."""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, quote, urlsplit

from bs4 import BeautifulSoup

from scrapers.full_import.department_stores import PublicScraper, SourceUnavailable, classification, inventory_row, number, text, timestamps, taxonomy
from scrapers.source_http import SourcePayloadError, StoreRateLimited


def variations(product, attribute):
    matches = [a for a in product.get('variationAttributes', []) if a.get('id') == attribute]
    if len(matches) != 1 or not matches[0].get('values'):
        raise ValueError(f'Missing Magasin {attribute} enumeration')
    return matches[0]['values']


def size_variants(product):
    """Return concrete sizes; the colour picker can list nonexistent combinations.

    Hidden, unselectable options without an ID or URL are not inventory rows.
    Their inStock flag is unreliable (observed true on nonexistent sizes).
    """
    result = []
    for variant in variations(product, 'size'):
        if not variant.get('productId'):
            if (variant.get('showProduct') is False and variant.get('selectable') is False
                    and not variant.get('url')):
                continue
            raise ValueError('Missing Magasin size identity for a published/selectable size')
        result.append(variant)
    if not result:
        raise ValueError('No concrete Magasin size variants')
    return result


def source_path(product):
    raw = product.get('koralCategoryPath')
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [text(v) for v in (raw or {}).get('category', '').split('>') if text(v)]


def prices(product):
    price = product['price']
    if price.get('type') == 'range':
        if any(not isinstance(price.get(k), dict) or price[k].get('type') != 'singlePrice' for k in ('min', 'max')):
            raise ValueError('Missing/invalid Magasin price range bounds')
        minimum, _ = prices({'price': price['min']})
        maximum, _ = prices({'price': price['max']})
        if minimum > maximum:
            raise ValueError('Reversed Magasin price range')
        # A colour row's scalar price is the advertised starting price. Only
        # retain a former price when the complete range shares that list price.
        lists = [price[k].get('list') for k in ('min', 'max')]
        regular = lists[0]['value'] if all(lists) and lists[0]['value'] == lists[1]['value'] else None
        return minimum, regular if regular is not None and regular > minimum else None
    if price.get('type') != 'singlePrice':
        raise ValueError('Unsupported Magasin price range')
    current = price.get('sales') or price.get('list')
    regular = price.get('list')
    if not current or current.get('currency') != 'DKK' or (regular and regular.get('currency') != 'DKK'):
        raise ValueError('Missing/invalid Magasin price')
    amount = number(current.get('value'))
    former = number(regular['value']) if regular else None
    return amount, former if former is not None and former > amount else None


def parse_stock(html, pid):
    soup = BeautifulSoup(html, 'html.parser')
    panel = soup.select_one('stock-availability-content')
    if panel is None or panel.get('data-pid') != pid:
        raise ValueError('Missing or wrong Magasin size stock response')
    matches = []
    for item in panel.select('li.stock-availability-overlay__item'):
        label = item.select_one('.stock-availability-overlay__item-name')
        if label and text(label.get_text()) == 'Magasin Aarhus':
            message = item.select_one('.availability-message')
            if not message:
                raise ValueError('Missing Magasin availability notice')
            notice = text(message.get_text()).casefold()
            if notice == 'ikke på lager':
                matches.append(False)
            elif notice in ('på lager', 'få på lager'):
                matches.append(True)
            else:
                raise ValueError(f'Unrecognized Magasin stock notice: {notice}')
    if len(matches) != 1:
        raise ValueError('Missing/ambiguous Magasin Aarhus inventory; preserve previous stock')
    return {'available': matches[0], 'stock': None}


def specs(product):
    result = {}
    for group in product.get('attributes', []):
        for item in group.get('attributes', []):
            if item.get('label') and item.get('value'):
                result[item['label']] = [text(v) for v in item['value']]
    for item in product.get('specifications', []):
        if item.get('label') and item.get('value'):
            result[item['label']] = [text(v) for v in item['value']]
    return result


class MagasinScraper(PublicScraper):
    BASE_URL = 'https://www.magasin.dk'
    API = '/on/demandware.store/Sites-DK-Site/da_DK/'

    def discover_product_urls(self):
        urls = set()
        for root in ('/herre/toej/', '/herre/sko/'):
            previous = set()
            for page in range(1, 2001):
                # The regular category route supports page=N; the internal AJAX route
                # sometimes serves a challenge rather than a catalog response.
                soup = BeautifulSoup(self.get(self.BASE_URL + root + f'?page={page}').text, 'html.parser')
                footer = soup.select_one('.grid-footer[data-page-number]')
                if footer is None or int(footer['data-page-number']) != page - 1:
                    raise ValueError(f'Invalid or repeated Magasin catalog page: {root} page {page}')
                count = re.search(r'Viser\s+([\d.]+)\s+ud af\s+([\d.]+)', footer.get_text(' ', strip=True))
                if not count:
                    raise ValueError('Missing Magasin catalog counts')
                shown, total = (int(v.replace('.', '')) for v in count.groups())
                links = {self.clean_url(a['href']) for a in soup.select('.productTile .b-productcard__media-link[href]')}
                # The last footwear page can contain no cards although its footer
                # explicitly reports completion (e.g. 1,189/1,189 on page 34).
                # Never accept an empty middle page or an empty first page.
                if not links and page > 1 and shown == total and page * float(footer['data-page-size']) >= total and footer.select_one('.load-more[data-callurl]') is None:
                    break
                if not links or links == previous:
                    raise ValueError(f'Empty or repeated Magasin product listing: {root} page {page}, {shown}/{total}')
                urls.update(links)
                previous = links
                if page == 1 or page % 25 == 0:
                    logging.getLogger(__name__).info('Magasin %s discovery page %s, colours=%s', root, page, len(urls))
                next_node = footer.select_one('.load-more[data-callurl]')
                if shown >= total:
                    break
                if next_node is None:
                    raise ValueError('Magasin pagination stopped before catalog end')
                next_page = parse_qs(urlsplit(next_node['data-callurl']).query).get('page')
                if next_page != [str(page + 1)]:
                    raise ValueError('Unexpected Magasin next page')
            else:
                raise ValueError('Magasin catalog exceeded pagination bound')
        return sorted(urls)

    def product(self, pid):
        response = self.get(self.BASE_URL + self.API + 'Product-Variation?pid=' + quote(pid, safe=''))
        try:
            payload = response.json()
        except ValueError as exc:
            if (response.headers.get('cf-mitigated') == 'challenge' or
                    any(marker in response.text[:2000].lower() for marker in ('just a moment...', 'challenges.cloudflare.com'))):
                raise StoreRateLimited('Magasin returned a verification challenge; stopping the store and retaining saved inventory.') from exc
            raise SourcePayloadError(
                f'Magasin variation {pid} returned invalid JSON: HTTP {response.status_code}, '
                f'content_type={response.headers.get("Content-Type", "unknown")}, '
                f'bytes={len(response.content)}. Product deferred; no inventory inferred.'
            ) from exc
        if not isinstance(payload, dict):
            raise SourcePayloadError(f'Magasin variation {pid} returned a non-object JSON payload.')
        p = payload.get('product')
        selected_size = (isinstance(p, dict) and p.get('colorVariantId') == pid and
                         p.get('id') in {v.get('productId') for a in p.get('variationAttributes', [])
                                        if a.get('id') == 'size' for v in a.get('values', [])})
        if not p or (p.get('id') != pid and not selected_size):
            raise ValueError('Magasin variation endpoint returned a different/missing product')
        # Product metadata only; discard tracking events and promotional fragments.
        fields = ('id','masterProductID','colorVariantId','productName','brand','productType',
                  'longDescription','shortDescription','topLevelCategoryID','price','variationAttributes',
                  'images','attributes','specifications','careInstructions','schemaMarkup',
                  'koralCategoryPath','colorName','sizeName','EAN','availability','available',
                  'isGiftCertificate','showStockAvailabilityOption')
        return {k: p.get(k) for k in fields}

    def color_url(self, product):
        # Some colour requests automatically select a size and return its URL.
        # Keep the stable colour URL for identity and checkpoint deduplication.
        url = self.clean_url(product['schemaMarkup']['url'])
        if not re.search(r'/[A-Za-z0-9-]+\.html$', url):
            raise ValueError('Invalid Magasin source product URL')
        return re.sub(r'/[A-Za-z0-9-]+\.html$', '/' + product['colorVariantId'] + '.html', url)

    def fetch_snapshot(self, url, full=True, *, color_ids=None):
        if color_ids is not None and (full or not color_ids):
            raise ValueError('Colour selection requires a nonempty refresh scope')
        match = re.search(r'/([A-Za-z0-9-]+)\.html$', self.clean_url(url))
        if not match:
            raise ValueError('Invalid Magasin product URL')
        first = self.product(match[1])
        if first.get('topLevelCategoryID') != 'mens' or not classification(source_path(first), first.get('productName')):
            return {'colors': [], 'excluded': True}
        master = first.get('masterProductID')
        if not master:
            raise ValueError('Missing Magasin master identity')
        colors = []
        options = (variations(first, 'color') if color_ids is None else
                   [{'productId': color_id} for color_id in dict.fromkeys(color_ids)])
        for option in options:
            pid = option.get('productId')
            if not pid:
                raise ValueError('Missing Magasin colour identity')
            try:
                p = first if pid in (first.get('id'), first.get('colorVariantId')) else self.product(pid)
            except SourceUnavailable as exc:
                raise ValueError('Magasin colour detail missing; incomplete family') from exc
            selected_option = (p.get('id') == pid and
                               pid in {v.get('productId') for v in variations(p, 'size')} and
                               (p.get('colorVariantId') or '').casefold() ==
                               f"{master}-{option.get('value') or option.get('id') or ''}".casefold())
            if p.get('masterProductID') != master or (p.get('colorVariantId') != pid and not selected_option):
                raise ValueError('Magasin colour family mismatch')
            pid = p['colorVariantId']
            prices(p)  # Validate before issuing stock and metadata requests.
            stock = {}
            size_prices = {}
            for variant in size_variants(p):
                vid = variant.get('productId')
                if not vid:
                    raise ValueError('Missing Magasin size identity')
                if p['price'].get('type') == 'range':
                    detail = self.product(vid)
                    if detail.get('masterProductID') != master or detail.get('colorVariantId') != pid:
                        raise ValueError('Magasin size price belongs to a different colour family')
                    if detail.get('price', {}).get('type') != 'singlePrice':
                        raise ValueError('Missing exact Magasin size price')
                    current, regular = prices(detail)
                    minimum, _ = prices({'price': p['price']['min']})
                    maximum, _ = prices({'price': p['price']['max']})
                    if not minimum <= current <= maximum:
                        raise ValueError('Magasin size price outside advertised range; retry this family')
                    size_prices[vid] = dict(current_price=current, list_price=regular)
                if p.get('showStockAvailabilityOption') is False:
                    stock[vid] = {'available': None, 'stock': None}
                else:
                    try:
                        stock_html = self.get(self.BASE_URL + self.API + 'Product-StockAvailability?pid=' + quote(vid, safe='')).text
                    except SourceUnavailable as exc:
                        raise ValueError('Magasin stock endpoint missing; not evidence of zero stock') from exc
                    stock[vid] = parse_stock(stock_html, vid)
            guide = {}
            if full:
                chunks = BeautifulSoup(self.get(self.BASE_URL + self.API + 'Product-Chunks?pid=' + quote(pid, safe='')).text, 'html.parser')
                tables = [[[text(c.get_text(' ',strip=True)) for c in tr.select('th,td')] for tr in t.select('tr')]
                          for t in chunks.select('.sizeguide table')]
                if tables:
                    guide = {'tables': tables}
            colors.append({'product': p, 'stock': stock, 'size_guide': guide, 'size_prices': size_prices})
        snapshot = {'colors': colors}
        self.rows_from_snapshot(snapshot, full=full)
        self.covered_urls.update(self.color_url(c['product']) for c in colors)
        return snapshot

    def rows_from_snapshot(self, snapshot, full=True):
        rows = []
        for entry in snapshot['colors']:
            p = entry['product']
            path = source_path(p)
            category = classification(path, p.get('productName'))
            if not category or p.get('topLevelCategoryID') != 'mens' or p.get('isGiftCertificate'):
                raise ValueError('Unexpected Magasin category in colour family')
            local, online = {}, []
            for variant in size_variants(p):
                size, vid = text(variant.get('displayValue')), variant.get('productId')
                if not size or size in local or type(variant.get('inStock')) is not bool:
                    raise ValueError('Invalid or duplicate Magasin size')
                local[size] = entry['stock'][vid]
                online.append({'size': size, 'available': variant['inStock'], 'stock': None,
                               'source_variant_id': vid, **entry.get('size_prices', {}).get(vid, {})})
                if p['price'].get('type') == 'range' and vid not in entry.get('size_prices', {}):
                    raise ValueError('Missing exact Magasin size price for ranged product')
            current, regular = prices(p)
            row = dict(source_parent_id=p['masterProductID'], source_color_id=p['colorVariantId'],
                       current_price=current, list_price=regular, webshop_sizes=online,
                       **inventory_row('magasin', 'Magasin Aarhus, Immervad 2–8', local, False), **timestamps())
            if full:
                specifications = specs(p)
                category, category_path = taxonomy(path)
                images = [v['urls']['xl'] for v in (p.get('images') or {}).get('zoom', []) if v.get('urls', {}).get('xl')]
                if not images or not text(p.get('productName')):
                    raise ValueError('Missing Magasin images/name')
                color = text(p.get('colorName')) or next((v['displayValue'] for v in variations(p,'color') if v['productId'] in (p['id'], p['colorVariantId'])), None)
                description = text(p.get('longDescription'))
                short = text(p.get('shortDescription'))
                if not description and short.casefold() != (color or '').casefold():
                    description = short
                row.update(source_url=self.color_url(p), canonical_url=self.color_url(p),
                           source_product_number=p['masterProductID'], name=text(p['productName']), brand=p.get('brand'),
                           product_type=path[-1], color=color, color_group=next(iter(specifications.get('Filter color', [])), None),
                           current_price=current, list_price=regular, currency='DKK', description=description or None,
                           materials=[v for k,vs in specifications.items() if 'material' in k.casefold() for v in vs],
                           fit=next(iter(specifications.get('Pasform', [])), None), category=category, category_path=category_path,
                           specifications={k:v for k,v in specifications.items() if k not in ('EAN','Størrelse','Ax numbers')},
                           images=list(dict.fromkeys(images)), size_guide=entry.get('size_guide', {}),
                           care_instructions=p.get('careInstructions') or [], raw={
                             'source_category_path': path,
                             'source_product': {k:p.get(k) for k in ('id','masterProductID','colorVariantId','EAN','price','showStockAvailabilityOption','schemaMarkup')},
                             'variations': {a['id']: [{k:v.get(k) for k in ('productId','displayValue','inStock','selectable','showProduct','url')} for v in a['values']]
                                            for a in p['variationAttributes']},
                             'availability_basis': 'public per-size Magasin Aarhus stock notice; source says updated once daily'})
            rows.append(row)
        return rows
