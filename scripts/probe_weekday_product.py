"""Read one Weekday colour and Aarhus size availability; never access a database.

Usage: .venv/bin/python scripts/probe_weekday_product.py [--url URL] [--headed]
Optional --output writes a validated observation, or a diagnostic batch report.
--urls-file accepts 1–10 URLs (one per line); --delay defaults to 3 seconds.
Batch reports separate failed requests from validated product observations.
Any failure returns exit code 1, including when a diagnostic report is saved.
Example: --urls-file tests/fixtures/weekday_urls.txt --output /tmp/weekday-batch.json
--input replays a captured snapshot offline (and labels the result as replay).

2026-10-07: the 10-page standalone Chromium batch returned 10 HTTP 403 responses.
An in-app browser comparison captured one stock panel and hit Access Denied on
the other nine pages. Headed Chromium's homepage visit returned a bot challenge.
Extraction was verified for the first product; unattended access is NOT
established. This probe stops on those responses; it does not solve challenges,
reuse personal cookies, scan the catalogue, or register a scheduled integration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

DEFAULT_URL = (
    "https://www.weekday.com/en-dk/p/men/jeans/loose-fit/"
    "astro-loose-baggy-leg-jeans-black-rinse-black-1114252035/"
)
STORE_KEY = "weekday-aarhus"
ADDRESS = "Guldsmedgade 10"

# Read only published page data and the rendered stock panel. Keep this capture
# independent of browser globals, cookies and framework internals.
SNAPSHOT_JS = r"""() => {
    const data = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
    const blocks = data.props.pageProps.blocks.filter(b => b.component === 'product-info');
    if (blocks.length !== 1) throw new Error('Expected one product-info block');
    const p = blocks[0].product;
    const dialogs = [...document.querySelectorAll('[role="dialog"][aria-modal="true"]')]
        .filter(d => d.textContent.includes('STORE AVAILABILITY'));
    if (dialogs.length !== 1) throw new Error('Expected one stock panel');
    const dialog = dialogs[0];
    const cards = [...dialog.querySelectorAll('div.border-b')].filter(d =>
        [...d.querySelectorAll('div')].some(n => n.textContent.trim() === 'Guldsmedgade 10')
        && /(?:Århus|Aarhus)/.test(d.textContent));
    if (cards.length !== 1) throw new Error('Aarhus stock card missing or ambiguous');
    const card = cards[0];
    const productLink = dialog.querySelector('[data-cy="product"] a');
    const product = {};
    for (const k of ['sku', 'productSku', 'uri', 'name', 'brandName', 'variantName',
                     'categoryNames', 'pricing', 'sizeVariants']) product[k] = p[k];
    return {
        url: location.origin + location.pathname, captured_at: new Date().toISOString(), product,
        inventory_url: productLink?.getAttribute('href'),
        store_address: 'Guldsmedgade 10', store_text: card.textContent,
        legend: [...dialog.querySelectorAll('span')].filter(s =>
            ['Available', 'Few left', 'Not available'].includes(s.textContent.trim()))
            .map(s => ({label: s.textContent.trim(), dot_class: s.previousElementSibling?.className})),
        sizes: [...card.querySelectorAll('span')].filter(s =>
            s.previousElementSibling?.classList.contains('rounded-full'))
            .map(s => ({label: s.textContent.trim(), dot_class: s.previousElementSibling.className}))
    };
}"""


class IncompleteObservation(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise IncompleteObservation(message)


def product_slug(url):
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.netloc == 'www.weekday.com',
            'Use a public https://www.weekday.com product URL')
    require(parsed.path.startswith('/en-dk/p/men/'), 'Probe scope is Danish men’s product URLs')
    require(not parsed.query and not parsed.fragment, 'Use a product URL without query or fragment')
    return parsed.path.rstrip('/').split('/')[-1]


def background_class(value):
    require(isinstance(value, str), 'Missing stock marker')
    colors = [c for c in value.split() if c.startswith('bg-')]
    require(len(colors) == 1, 'Missing or ambiguous stock marker colour')
    return colors[0]


def parse_snapshot(snapshot, *, replay=False):
    """Reject partial/mismatched observations rather than reporting zero stock."""
    p = snapshot['product']
    require(product_slug(snapshot['url']) == p['uri'], 'Product URL/identity mismatch')
    require(re.fullmatch(r'\d{7}', p['productSku']) is not None, 'Invalid style ID')
    require(re.fullmatch(r'\d{10}', p['sku']) is not None and p['sku'].startswith(p['productSku']),
            'Invalid colour ID')
    require(urlsplit(snapshot['inventory_url']).path.rstrip('/').split('/')[-1] == p['uri'],
            'Stock panel belongs to another colour')
    require(snapshot['store_address'] == ADDRESS and ADDRESS in snapshot['store_text']
            and re.search(r'Århus|Aarhus', snapshot['store_text']), 'Wrong or missing Aarhus store')
    require('men' in p['categoryNames'], 'Product is outside the men’s catalogue')
    for field in ('name', 'brandName', 'variantName'):
        require(isinstance(p[field], str) and p[field].strip(), f'Missing {field}')
    captured = datetime.fromisoformat(snapshot['captured_at'].replace('Z', '+00:00'))
    require(captured.tzinfo is not None, 'Capture timestamp must include a timezone')
    price = p['pricing']['priceAsNumber']
    require(type(price) in (float, int) and math.isfinite(price) and price >= 0,
            'Invalid public price')
    require(re.fullmatch(r'\s*[\d,. ]+\s*DKK\s*', p['pricing']['price']) is not None,
            'Expected DKK price')

    status_by_class = {}
    statuses = {'Available': True, 'Few left': True, 'Not available': False}
    require(len(snapshot['legend']) == 3, 'Incomplete availability legend')
    for item in snapshot['legend']:
        require(item['label'] in statuses, 'Unknown availability status')
        color = background_class(item['dot_class'])
        require(color not in status_by_class, 'Ambiguous availability legend')
        status_by_class[color] = item['label']
    require(set(status_by_class.values()) == set(statuses), 'Incomplete availability legend')

    expected = [v['label'] for v in p['sizeVariants']]
    require(expected and all(isinstance(s, str) and s.strip() for s in expected)
            and len(expected) == len(set(expected)), 'Missing or duplicate product sizes')
    sizes, source_status = {}, {}
    for item in snapshot['sizes']:
        label = item['label']
        require(label not in sizes, 'Duplicate local size')
        color = background_class(item['dot_class'])
        require(color in status_by_class, 'Unrecognized local stock marker')
        status = status_by_class[color]
        sizes[label] = {'available': statuses[status], 'stock': None}
        source_status[label] = status
    require(set(sizes) == set(expected), 'Local sizes do not match the complete product size set')
    available = any(s['available'] for s in sizes.values())
    return {
        'observation_mode': 'replay' if replay else 'live',
        'observed_at': captured.astimezone(timezone.utc).isoformat(),
        'source_parent_id': p['productSku'], 'source_color_id': p['sku'],
        'canonical_url': snapshot['url'], 'name': p['name'], 'brand': p['brandName'],
        'color': p['variantName'], 'current_price': price, 'currency': 'DKK',
        'local_inventory': {'stores': {STORE_KEY: {
            'name': 'Weekday Aarhus, Guldsmedgade 10', 'stock_known': False,
            'available': available, 'total_stock': None, 'sizes': sizes,
        }}},
        'local_available': available, 'aarhus_available': available,
        'local_total_stock': None, 'aarhus_total_stock': None,
        'raw': {'local_size_status': source_status},
    }


class AccessBlocked(IncompleteObservation):
    def __init__(self, message, http_status=None):
        super().__init__(message)
        self.http_status = http_status


def capture_page(page, url):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    product_slug(url)
    response = page.goto(url, wait_until='domcontentloaded')
    status = response.status if response else None
    if status in (401, 403, 429):
        raise AccessBlocked(f'Product HTTP {status}', status)
    require(status == 200, f'Product HTTP {status}; no observation emitted')
    if page.locator('#sec-if-cpt-container').count():
        raise AccessBlocked('Source returned a bot-check page', status)
    try:
        page.wait_for_function("document.getElementById('__NEXT_DATA__') !== null")
        cookies = page.get_by_role('button', name='Only required cookies', exact=True)
        for button in cookies.all():
            if button.is_visible():
                button.click()
                break
        with page.expect_response(lambda r: urlsplit(r.url).path == '/api/inventory/') as pending:
            page.get_by_role('button', name='Find in store', exact=True).click()
        status = pending.value.status
        if status in (401, 403, 429):
            raise AccessBlocked(f'Inventory HTTP {status}', status)
        require(status == 200, 'Inventory request failed; no observation emitted')
        dialog = page.get_by_role('dialog').filter(has=page.get_by_role('heading', name='STORE AVAILABILITY'))
        # A fully sold-out store must not disappear due to the default filter.
        dialog.get_by_role('checkbox', name='Only show store with stock', exact=True).uncheck()
        dialog.get_by_text(ADDRESS, exact=True).wait_for()
        return page.evaluate(SNAPSHOT_JS)
    except PlaywrightTimeout as exc:
        raise IncompleteObservation('Product/stock panel did not load completely; no observation emitted') from exc


def capture(url, *, headed=False, timeout=30):
    from playwright.sync_api import sync_playwright
    product_slug(url)
    with sync_playwright() as pw:
        # Full Chromium also supports new headless mode, keeping both paths similar.
        browser = pw.chromium.launch(headless=not headed, channel='chromium')
        try:
            page = browser.new_page(locale='en-DK')
            page.set_default_timeout(timeout * 1000)
            return capture_page(page, url)
        finally:
            browser.close()


def read_urls(path):
    urls = [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')]
    require(1 <= len(urls) <= 10, 'Batch must contain between 1 and 10 URLs')
    require(len(set(urls)) == len(urls), 'Batch contains duplicate URLs')
    for url in urls:
        product_slug(url)
    return urls


def observe_batch(page, urls, delay):
    """One attempt per page; errors never become product or zero-stock rows."""
    results = []
    for index, url in enumerate(urls):
        if index:
            time.sleep(delay)
        started = time.monotonic()
        entry = {'url': url}
        try:
            entry.update(status='success', observation=parse_snapshot(capture_page(page, url)))
        except AccessBlocked as exc:
            entry.update(status='blocked', error=str(exc), http_status=exc.http_status)
        except Exception as exc:
            entry.update(status='error', error=f'{type(exc).__name__}: {exc}')
        entry['elapsed_seconds'] = round(time.monotonic() - started, 2)
        results.append(entry)
        print(f"[{index + 1}/{len(urls)}] {entry['status']}: {url}", file=sys.stderr, flush=True)
        # Respect explicit rate limiting rather than continuing the sample.
        if entry.get('http_status') == 429:
            results.extend({'url': rest, 'status': 'skipped', 'error': 'Stopped after HTTP 429'}
                           for rest in urls[index + 1:])
            break
    return {
        'completed_at': datetime.now(timezone.utc).isoformat(),
        'transport': 'standalone-playwright',
        'requested': len(urls),
        'succeeded': sum(r['status'] == 'success' for r in results),
        'blocked': sum(r['status'] == 'blocked' for r in results),
        'errors': sum(r['status'] == 'error' for r in results),
        'skipped': sum(r['status'] == 'skipped' for r in results),
        'results': results,
    }


def capture_batch(urls, *, headed=False, timeout=30, delay=3):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, channel='chromium')
        try:
            page = browser.new_page(locale='en-DK')
            page.set_default_timeout(timeout * 1000)
            report = observe_batch(page, urls, delay)
            report.update(browser_version=browser.version, headed=headed, delay_seconds=delay)
            return report
        finally:
            browser.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--url', help='One Danish Weekday men’s product URL')
    source.add_argument('--urls-file', type=Path, help='1–10 product URLs, one per line')
    source.add_argument('--input', type=Path, help='Offline capture JSON; never treated as live')
    parser.add_argument('--headed', action='store_true', help='Use ordinary Chromium instead of headless')
    parser.add_argument('--timeout', type=int, default=30, help='Per-operation timeout in seconds (1–120)')
    parser.add_argument('--output', type=Path, help='Write result or diagnostic batch report')
    parser.add_argument('--delay', type=float, default=3, help='Batch delay in seconds (1–60)')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 120:
        parser.error('--timeout must be between 1 and 120 seconds')
    if not 1 <= args.delay <= 60:
        parser.error('--delay must be between 1 and 60 seconds')
    try:
        if args.urls_file:
            result = capture_batch(read_urls(args.urls_file), headed=args.headed,
                                   timeout=args.timeout, delay=args.delay)
        else:
            snapshot = json.loads(args.input.read_text()) if args.input else capture(
                args.url or DEFAULT_URL, headed=args.headed, timeout=args.timeout)
            result = parse_snapshot(snapshot, replay=args.input is not None)
        output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            args.output.write_text(output)
        else:
            print(output, end='')
        return int(bool(args.urls_file) and result['succeeded'] != result['requested'])
    except Exception as exc:
        print(f'Weekday probe failed ({type(exc).__name__}): {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
