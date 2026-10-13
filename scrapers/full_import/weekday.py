"""Public Weekday catalog/price and physical inventory parsing; no database writes."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import re
import time
from urllib.parse import quote, urlencode, urlsplit

PROFILE = 'safari184'  # Pin the tested identity; do not rotate headers independently.
STORE_ID = 'DK0651'
STORE_KEY = 'weekday-aarhus'
STORE_ADDRESS = 'Guldsmedgade 10'
POSSIBLE_LIMIT = 100
DYNAMIC_FIELDS = {
    'current_price', 'list_price', 'currency', 'local_inventory',
    'local_total_stock', 'local_available', 'aarhus_total_stock', 'aarhus_available',
    'scraped_at',
}


class ObservationError(ValueError):
    pass


class Blocked(ObservationError):
    pass


def require(condition, message):
    if not condition:
        raise ObservationError(message)


def slug_from_url(url):
    u = urlsplit(url)
    require(u.scheme == 'https' and u.netloc == 'www.weekday.com'
            and u.path.startswith('/en-dk/p/') and not u.query and not u.fragment,
            'Expected a Danish Weekday product URL without query or fragment')
    slug = u.path.rstrip('/').split('/')[-1]
    require(bool(re.fullmatch(r'[a-z0-9-]+', slug)), 'Invalid product slug')
    return slug


def product_from_response(payload, url):
    require(isinstance(payload, dict) and payload.get('statusCode') == 200, 'Invalid detail envelope')
    data = payload['data']
    require(data.get('marketCode') == 'dk' and data.get('languageCode') == 'en', 'Wrong market/language')
    product = data['product']
    require(product['uri'] == slug_from_url(url), 'Product slug changed')
    require(re.fullmatch(r'\d{7}', product['productSku']) is not None, 'Invalid style ID')
    require(re.fullmatch(r'\d{10}', product['sku']) is not None
            and product['sku'].startswith(product['productSku']), 'Invalid article ID')
    require('men' in product['categoryNames'], 'Product is outside the men’s catalog')
    require(product['brandName'] in ('Weekday', 'Cheap Monday'), 'Unknown storefront brand')
    for key in ('name', 'variantName'):
        require(isinstance(product[key], str) and bool(product[key].strip()), f'Missing {key}')
    size_map(product)
    return product


def size_map(product):
    sizes = {}
    labels = set()
    for variant in product['sizeVariants']:
        sku, label = variant['sku'], variant['label']
        require(isinstance(sku, str) and re.fullmatch(r'\d{13}', sku) is not None
                and sku.startswith(product['sku']), 'Invalid size SKU')
        require(sku not in sizes and isinstance(label, str) and label.strip()
                and label not in labels, 'Duplicate or missing size identity/label')
        sizes[sku] = label
        labels.add(label)
    require(bool(sizes), 'No product sizes')
    return sizes


def money(value):
    require(type(value) in (int, float), 'Price must be numeric')
    try:
        number = Decimal(str(value))
        require(number.is_finite() and number >= 0, 'Invalid price')
        require(number == number.quantize(Decimal('0.01')), 'Unexpected price precision')
        return number
    except InvalidOperation as exc:
        raise ObservationError('Invalid price') from exc


def price_fields(product):
    pricing = product['pricing']
    current = money(pricing['priceAsNumber'])
    displayed = re.fullmatch(r'\s*([0-9]+(?:\.[0-9]{1,2})?)\s+DKK\s*', pricing['price'])
    require(displayed and Decimal(displayed[1]) == current, 'Price text/numeric/currency mismatch')
    previous = money(pricing['priceBeforeDiscountAsNumber'])
    voucher = product.get('voucher') or {}
    percentage = voucher.get('voucherPercentage')
    require(percentage is None or str(percentage) in ('', '0', '0.0'),
            'Voucher-adjusted price requires validation; preserve previous price')
    return {'current_price': float(current),
            'list_price': float(previous) if previous > current else None, 'currency': 'DKK'}


def local_fields(product, inventory):
    require(isinstance(inventory, list), 'Inventory is not a list')
    sizes = size_map(product)
    local = []
    for entry in inventory:
        require(entry.get('articleId') == product['sku'], 'Wrong inventory article')
        store = entry.get('store')
        require(isinstance(store, dict) and isinstance(store.get('storeCode'), str), 'Missing store identity')
        if store['storeCode'] != STORE_ID:
            continue
        require(store.get('countryCode') == 'DK' and store.get('storeClass') == 'Weekday'
                and store.get('address', {}).get('streetName1') == STORE_ADDRESS
                and store.get('city') in ('Århus C', 'Aarhus C'), 'Aarhus store identity changed')
        quantity = entry.get('quantity')
        require(type(quantity) is int and quantity >= 0, 'Invalid local quantity')
        variant = entry.get('variantId')
        # The website emits these rows for a store with no size-level stock.
        require(variant in sizes or (variant == '' and quantity == 0)
                or (isinstance(variant, str) and re.fullmatch(r'\d{13}', variant)
                    and variant.startswith(product['sku'])), 'Unknown Aarhus size SKU')
        local.append(entry)

    flags = []
    unmapped = [entry for entry in local if entry['variantId'] and entry['variantId'] not in sizes]
    if unmapped:
        flags.append('unmapped_aarhus_sizes')
    possibly_capped = len(inventory) >= POSSIBLE_LIMIT
    if possibly_capped:
        flags.append('possible_inventory_truncation')
    if not local:
        flags.append('aarhus_store_missing')
    complete = bool(local) and not possibly_capped and not unmapped
    quantities = {}
    for entry in local:
        if entry['variantId'] == '':
            continue
        require(entry['variantId'] not in quantities, 'Duplicate Aarhus size SKU')
        quantities[entry['variantId']] = entry['quantity']
    normalized = {}
    for sku, label in sizes.items():
        stock = quantities.get(sku, 0 if complete else None)
        normalized[label] = {'available': stock > 0 if stock is not None else None, 'stock': stock}
    if any(v['available'] is True for v in normalized.values()) or any(v['quantity'] > 0 for v in unmapped):
        available = True
    elif any(v['available'] is None for v in normalized.values()):
        available = None
    else:
        available = False
    exact = complete and all(v['stock'] is not None for v in normalized.values())
    known_total = sum(quantities.values())
    total = known_total if exact else None
    store = {'name': 'Weekday Aarhus, Guldsmedgade 10', 'stock_known': exact,
             'available': available, 'total_stock': total, 'sizes': normalized}
    fields = {'local_inventory': {'stores': {STORE_KEY: store}},
              'local_total_stock': known_total if local else None,
              'local_available': available, 'aarhus_total_stock': total, 'aarhus_available': available}
    return fields, flags


def dynamic_fields(product, inventory, observed_at):
    fields = price_fields(product)
    local, flags = local_fields(product, inventory)
    fields.update(local, scraped_at=observed_at)
    return fields, flags


def catalog_row(product, inventory, url, observed_at):
    fields, flags = dynamic_fields(product, inventory, observed_at)
    row = {
        'source_parent_id': product['productSku'], 'source_color_id': product['sku'],
        'canonical_url': url, 'name': product['name'], 'brand': product['brandName'],
        'color': product['variantName'], 'category_path': product['categoryNames'],
        'description_html': product.get('description'), 'images': product.get('media', {}).get('images', []),
        'material': product.get('material'), 'fit': product.get('fitAttributes'),
        'source_sizes': size_map(product), 'source_metadata': product,
        'source_inventory': {'store_id': STORE_ID, 'rows': [
            {'variant_id': entry['variantId'], 'quantity': entry['quantity']}
            for entry in inventory if entry['store']['storeCode'] == STORE_ID]},
        **fields,
    }
    return row, flags


def refresh_patch(existing, product, inventory, observed_at):
    require(existing['source_color_id'] == product['sku']
            and existing['source_parent_id'] == product['productSku'], 'Known product identity changed')
    fields, flags = dynamic_fields(product, inventory, observed_at)
    # Do not overwrite any previous size/summary with a partial response. A valid
    # price can still refresh independently when local stock completeness is unknown.
    if flags:
        fields = {k: v for k, v in fields.items() if k in ('current_price', 'list_price', 'currency')}
    require(set(fields) <= DYNAMIC_FIELDS, 'Attempt to refresh stable metadata')
    result = {'source_parent_id': existing['source_parent_id'],
              'source_color_id': existing['source_color_id'], 'observed_at': observed_at, 'patch': fields}
    if 'id' in existing:
        result['id'] = existing['id']
    return result, flags


class Client:
    def __init__(self, *, delay=3, timeout=25):
        from curl_cffi import requests
        self.session = requests.Session(impersonate=PROFILE)
        self.delay, self.timeout, self.last_request = delay, timeout, None
        self.diagnostics = []

    def close(self):
        self.session.close()

    def get_json(self, url):
        if self.last_request is not None:
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        response = self.session.get(url, timeout=self.timeout, allow_redirects=False)
        diagnostic = {'url': url, 'http_status': response.status_code,
                      'received_at': datetime.now(timezone.utc).isoformat(),
                      'content_type': response.headers.get('content-type'),
                      'cache_control': response.headers.get('cache-control'),
                      'date': response.headers.get('date'), 'age': response.headers.get('age'),
                      'bytes': len(response.content),
                      'body_sha256': hashlib.sha256(response.content).hexdigest()}
        self.diagnostics.append(diagnostic)
        if response.status_code in (401, 403, 429) or 'sec-if-cpt-container' in response.text:
            raise Blocked(f'Blocked HTTP {response.status_code}; stop this run and retry later')
        require(response.status_code == 200, f'HTTP {response.status_code}; no disappearance inferred')
        require('application/json' in (response.headers.get('content-type') or ''), 'Expected JSON, got a page')
        return response.json()

    def fetch_detail(self, url):
        slug = slug_from_url(url)
        payload = self.get_json('https://api.weekday.com/product-detail/v1/dk/products/'
                                + quote(slug, safe='') + '?languageCode=en')
        product = product_from_response(payload, url)
        return product

    def fetch_inventory(self, product):
        return self.get_json('https://www.weekday.com/api/inventory/?' + urlencode({
            'article': product['sku'], 'brand': product['brandName'], 'country': 'dk', 'locale': 'en_dk'}))

    def fetch(self, url):
        product = self.fetch_detail(url)
        return product, self.fetch_inventory(product)



def in_scope(product):
    """Men's clothing, including unisex items; exclude canonical accessories."""
    return ('men' in product.get('categoryNames', [])
            and product.get('brandName') in ('Weekday', 'Cheap Monday')
            and '/accessories' not in '/' + product.get('categoryUri', ''))


def canonical_url(product):
    url = product['url']
    if url.startswith('/en-dk/p/'):
        url = 'https://www.weekday.com' + url
    slug_from_url(url)
    return url.rstrip('/') + '/'


def discover_catalog(client):
    """Use the storefront's public listing API, including its last-chance range."""
    import uuid
    session_key = str(uuid.uuid4())
    products, pages, observed_times = {}, [], {}
    for category in ('view-all', 'last-chance'):
        offset, expected, seen = 0, None, set()
        while True:
            query = urlencode({'customerKey': session_key, 'sessionKey': session_key,
                               'locale': 'en-GB', 'touchpoint': 'DESKTOP',
                               'pageReference': '/men/' + category, 'notify': 'false',
                               'limit': 100, 'skip': offset})
            payload = client.get_json('https://api.weekday.com/products/v1/dk/products?' + query)
            require(payload.get('statusCode') == 200, 'Invalid catalog envelope')
            data = payload['data']
            pagination, batch = data['pagination'], data['products']
            total = pagination['totalProducts']
            require(type(total) is int and 0 < total <= 20000, 'Invalid catalog total')
            if expected is None:
                expected = total
            require(total == expected and pagination['offset'] == offset,
                    'Catalog changed during pagination; retry discovery')
            ids = {p['sku'] for p in batch}
            require(batch and len(ids) == len(batch) and not ids.intersection(seen),
                    'Catalog pagination repeated or omitted products')
            require(offset + len(batch) <= total, 'Catalog exceeds published total')
            for product in batch:
                # Validate identities and sizes before trusting discovery for stock queries.
                product_from_response({'statusCode': 200, 'data': {
                    'marketCode': 'dk', 'languageCode': 'en', 'product': product}}, canonical_url(product))
                product = dict(product)
                product.pop('ticket', None)  # recommendation tracking, not product metadata
                products[product['sku']] = product
                observed_times[product['sku']] = datetime.now(timezone.utc).isoformat()
            seen.update(ids)
            pages.append({'category': category, 'offset': offset, 'count': len(batch), 'total': total})
            offset += len(batch)
            if offset == total:
                break
    return {'schema_version': 1, 'mode': 'discovery', 'complete': True,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'pages': pages,
            'products': list(products.values()), 'product_observed_at': observed_times}
