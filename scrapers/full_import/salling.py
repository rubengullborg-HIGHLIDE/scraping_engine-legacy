"""Salling style/colour imports from public Nuxt product data, Aarhus only."""
from __future__ import annotations

import json
import re
import unicodedata
from copy import deepcopy
import logging
from collections import OrderedDict

from bs4 import BeautifulSoup

from scrapers.full_import.department_stores import PublicScraper, SourceUnavailable, classification, integer, inventory_row, number, text, timestamps, taxonomy


def decode_nuxt(html):
    node = BeautifulSoup(html, 'html.parser').select_one('#__NUXT_DATA__')
    if node is None:
        raise ValueError('Missing Salling product payload')
    values = json.loads(node.string or node.get_text())
    cache = {}

    def decode(index):
        if type(index) is not int:
            raise ValueError('Invalid Nuxt reference')
        if index in (-1, -2):  # undefined / sparse-array hole
            return None
        if index < 0 or index >= len(values):
            raise ValueError('Unsupported Nuxt constant')
        if index in cache:
            return cache[index]
        value = values[index]
        if isinstance(value, dict):
            cache[index] = {}
            cache[index].update({k: decode(v) for k, v in value.items()})
        elif isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in ('EmptyRef', 'EmptyShallowRef'):
                    return None
                if tag not in ('Reactive', 'ShallowReactive', 'Ref', 'ShallowRef') or len(value) != 2:
                    raise ValueError(f'Unsupported Nuxt type: {tag}')
                cache[index] = decode(value[1])
            else:
                cache[index] = []
                cache[index].extend(decode(v) for v in value)
        else:
            cache[index] = value
        return cache[index]
    return decode(0)


def product_from_html(html):
    root = decode_nuxt(html)
    products = []
    def visit(node):
        if isinstance(node, dict):
            if 'parentId' in node and 'variantAttributes' in node:
                products.append(node)
                return
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    # Only page content, never recommendations, global navigation or analytics.
    for page in root.get('data', {}).values():
        if isinstance(page, dict):
            visit(page.get('content', {}))
    if len(products) != 1:
        raise ValueError(f'Expected one Salling page product, received {len(products)}')
    return products[0]


def attribute_values(product, name):
    return list(dict.fromkeys(text(a.get('value')) for a in product.get('attributes', [])
                             if a.get('attributeType', {}).get('name', '').casefold() == name.casefold() and text(a.get('value'))))


def color_key(product):
    values = attribute_values(product, 'Variantfarve')
    if len(values) != 1:
        raise ValueError('Missing or ambiguous Salling colour')
    return unicodedata.normalize('NFKC', values[0]).casefold()


def size_label(product):
    values = attribute_values(product, 'Variantstørrelse')
    if len(values) != 1:
        raise ValueError('Missing or ambiguous Salling size')
    return values[0]


def prices(product):
    tag = product['currentPriceTag']
    if tag['total']['currency'] != 'DKK' or tag['unitPrice']['currency'] != 'DKK':
        raise ValueError('Unexpected Salling currency')
    current, regular = number(tag['total']['value']), number(tag['unitPrice']['value'])
    return current, regular if regular > current else None


class SallingScraper(PublicScraper):
    BASE_URL = 'https://salling.dk'

    def discover_product_urls(self):
        # Live listings choose a current representative size; the sitemap contains
        # tens of thousands of size URLs, including disabled/deleted detail pages.
        families = {}
        for root in ('/herre/toej/c-11905/', '/herre/sko/c-1002/'):
            previous = None
            for page_number in range(1, 2001):
                payload = decode_nuxt(self.get(self.BASE_URL + root + f'?page={page_number}').text)
                candidates = [v for v in payload.get('data', {}).values() if isinstance(v, dict)]
                if len(candidates) != 1:
                    raise ValueError('Missing Salling listing data')
                listing = candidates[0]['content']['content']['categoryDetails']['data']['productList']
                result = listing['products']
                if listing['page'] != page_number or not result['results']:
                    raise ValueError('Invalid Salling pagination')
                ids = tuple(p.get('id') for p in result['results'] if p.get('parentId'))
                if not ids or ids == previous:
                    raise ValueError('Empty/repeated Salling listing')
                previous = ids
                for product in result['results']:
                    if product.get('parentId') and re.search(r'/herre/(?:toej|sko)/.+/p-\d+/?$', product.get('link') or ''):
                        families.setdefault(str(product['parentId']), self.clean_url(product['link']))
                if page_number == 1 or page_number % 25 == 0:
                    logging.getLogger(__name__).info('Salling %s discovery page %s/%s, styles=%s', root, page_number, result['totalPages'], len(families))
                if page_number >= result['totalPages']:
                    break
            else:
                raise ValueError('Salling pagination exceeded bound')
        if not families:
            raise ValueError('Empty Salling catalog')
        return sorted(families.values())

    def fetch_product(self, url):
        product = product_from_html(self.get(url).text)
        expected = re.search(r'/p-(\d+)/?$', self.clean_url(url))
        if not expected or str(product.get('id')) != expected.group(1):
            raise ValueError('Salling returned a different product')
        return product

    def fetch_snapshot(self, url, full=True):
        first = self.fetch_product(url)
        path = [v['name'] for v in first.get('breadCrumbsData', [])]
        if not classification(path, first.get('title')):
            return {'products': [], 'excluded': True}
        parent = first.get('parentId')
        if not parent:
            raise ValueError('Missing Salling style identity')
        fetched = {str(first['id']): first}
        products = {}
        colors = first.get('variantAttributes', {}).get('farve', [])
        if not colors:
            raise ValueError('Missing Salling colour enumeration')
        for color in colors:
            pid = str(color['productId'])
            try:
                representative = fetched.get(pid) or self.fetch_product(color['productUrl'])
            except SourceUnavailable as exc:
                raise ValueError('Salling colour detail missing; incomplete family') from exc
            fetched[pid] = representative
            variants = representative.get('variantAttributes', {}).get('størrelse', [])
            if not variants:
                raise ValueError('Missing Salling size enumeration')
            for variant in variants:
                vid = str(variant['productId'])
                detail = None
                try:
                    detail = fetched.get(vid) or self.fetch_product(variant['productUrl'])
                    fetched[vid] = detail
                except SourceUnavailable as exc:
                    if variant.get('isAvailable') is not False:
                        raise ValueError('Enabled Salling size detail missing') from exc
                if detail is not None and detail.get('parentId') != parent:
                    raise ValueError('Salling family mismatch')
                if detail is None or color_key(detail) != color_key(representative):
                    if variant.get('isAvailable') is not False:
                        raise ValueError('Available Salling size points to a different colour')
                    # Disabled combinations sometimes point to another colour's size
                    # (or a 404). Do not borrow that other colour's stock.
                    detail = deepcopy(representative)
                    detail.update(id=variant['productId'], url=variant['productUrl'],
                                  currentPriceTag=variant['currentPriceTag'],
                                  availableQuantity=variant['availableQuantity'],
                                  isBuyable=variant['isBuyable'], ean=None,
                                  indexableStockInfo=[], _source_page_unavailable=True)
                    detail['attributes'] = [a for a in detail['attributes']
                                            if a.get('attributeType', {}).get('name') != 'Variantstørrelse']
                    detail['attributes'].append(variant['attribute'])
                if size_label(detail) != text(variant['attribute']['value']):
                    raise ValueError('Salling size selection mismatch')
                products[(color_key(representative), vid)] = detail
        snapshot = {'products': list(products.values())}
        # Validate the complete snapshot before allowing the importer to skip covered URLs.
        self.rows_from_snapshot(snapshot, full=full)
        self.covered_urls.update(self.clean_url(p['url']) for p in products.values())
        return snapshot

    def rows_from_snapshot(self, snapshot, full=True):
        groups = OrderedDict()
        for p in snapshot['products']:
            key = (str(p['parentId']), color_key(p))
            groups.setdefault(key, []).append(p)
        rows = []
        for (parent, color), products in groups.items():
            p = next((v for v in products if not v.get('_source_page_unavailable')), None)
            if p is None:
                raise ValueError('No readable Salling colour page')
            path = [b['name'] for b in p['breadCrumbsData']]
            category = classification(path, p['title'])
            if not category:
                raise ValueError('Unexpected product category in Salling colour family')
            local, online, readable_prices, available_prices = {}, [], [], []
            for variant in products:
                size = size_label(variant)
                if size in local:
                    raise ValueError(f'Duplicate Salling size {size}')
                stores = [s for s in variant.get('indexableStockInfo', []) if s.get('name') == 'Salling Aarhus']
                if len(stores) != 1 and not variant.get('_source_page_unavailable'):
                    raise ValueError('Missing Salling Aarhus stock; preserve previous inventory')
                stock = integer(stores[0].get('availableStock')) if stores else None
                local[size] = {'available': stock > 0 if stock is not None else None, 'stock': stock}
                online_stock = integer(variant.get('availableQuantity'))
                current, regular = prices(variant)
                online.append({'size': size, 'available': online_stock > 0 and variant.get('isBuyable') is True,
                               'stock': online_stock, 'source_variant_id': str(variant['id']),
                               'ean': variant.get('ean'), 'url': self.clean_url(variant['url']),
                               'current_price': current, 'list_price': regular})
                if not variant.get('_source_page_unavailable'):
                    readable_prices.append(online[-1])
                    if online[-1]['available'] or local[size]['available'] is True:
                        available_prices.append(online[-1])
            expected = {text(v['attribute']['value']) for v in p['variantAttributes']['størrelse']}
            if set(local) != expected:
                raise ValueError('Incomplete Salling size coverage')
            cheapest = min(available_prices or readable_prices, key=lambda v: v['current_price'])
            row = dict(source_parent_id=parent, source_color_id=color,
                       current_price=cheapest['current_price'], list_price=cheapest['list_price'],
                       webshop_sizes=online, **inventory_row('salling', 'Salling Aarhus, Søndergade 27', local,
                                                            all(s['stock'] is not None for s in local.values())), **timestamps())
            if full:
                category, category_path = taxonomy(path)
                exact_color = attribute_values(p, 'Variantfarve')[0]
                title = text(p['title'])
                # Source title includes the selected size. Remove only exact suffixes.
                for suffix in (', ' + size_label(p), ', ' + exact_color):
                    if title.endswith(suffix):
                        title = title[:-len(suffix)]
                specifications = {}
                for a in p.get('attributes', []):
                    typ = a.get('attributeType', {})
                    if typ.get('displayOnWebsite') and typ.get('name') and 'størrelse' not in typ['name'].casefold():
                        specifications.setdefault(typ['name'], []).append(text(a.get('value')))
                images = list(dict.fromkeys(i.get('cdnUrl') or i.get('url') for i in p.get('images', [])
                                             if not i.get('isVideo') and (i.get('cdnUrl') or i.get('url'))))
                if not title or not images:
                    raise ValueError('Incomplete Salling name/images')
                row.update(source_url=self.clean_url(p['url']), canonical_url=self.clean_url(p['url']),
                           name=title, brand=p.get('manufacturerName'),
                           product_type=p.get('primaryCategoryName'), color=exact_color,
                           color_group=', '.join(dict.fromkeys(text(a.get('value')) for a in p.get('attributes', [])
                                 if a.get('attributeType', {}).get('name') == 'Farve' and a['attributeType'].get('displayOnWebsite'))) or None,
                           currency='DKK', category=category, category_path=category_path,
                           description=text(p.get('longDescription') or p.get('shortDescription')) or None,
                           materials=attribute_values(p, 'Materiale'), fit=next(iter(attribute_values(p, 'Pasform')), None),
                           specifications=specifications, images=images,
                           raw={'source_category_path': path, 'identity_basis': 'parentId + normalized Variantfarve',
                                'attributes': p.get('attributes'), 'instructions': p.get('instructions'),
                                'manufacturer_info': p.get('productManufacturerInfo'),
                                'variants': [{k:v.get(k) for k in ('id','erpId','ean','url','currentPriceTag','clubSallingPriceTag',
                                             'indexableStockInfo','secondaryStockInfo','availableQuantity','isBuyable','_source_page_unavailable')}
                                             for v in products],
                                'price_basis': 'lowest available readable size price, otherwise lowest readable size; member-only prices excluded'})
            rows.append(row)
        return rows
