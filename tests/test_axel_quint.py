from __future__ import annotations

import copy
import gzip
import json
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

from scrapers.full_import.axel_quint import AxelQuintScraper, IncompleteProduct, SourceUnavailable, formatted_price, parse_size_guide
from scripts.import_axel_quint_products import import_products, parse_args
from scripts.refresh_axel_quint_inventory import refresh_store
from scripts.refresh_store_inventory import STORE_SPECS

FIXTURES = Path(__file__).parent / 'fixtures' / 'axel_quint'


def fixture(store='axel', index=1):
    return json.loads((FIXTURES / f'{store}-{index}.json').read_text())


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.scraper = AxelQuintScraper('axel')
        self.addCleanup(self.scraper.close)

    def test_exact_local_inventory_is_separate_from_online(self):
        row = self.scraper.rows_from_snapshot(fixture())[0]
        self.assertEqual(row['aarhus_total_stock'], 12)
        store = row['local_inventory']['stores']['axel-aarhus']
        self.assertEqual(store['sizes']['W32'], {'available': True, 'stock': 3})
        self.assertEqual(set(row['local_inventory']['stores']), {'axel-aarhus'})
        self.assertEqual(row['local_total_stock'], row['aarhus_total_stock'])
        self.assertTrue(store['stock_known'])
        self.assertEqual(row['category'], 'Jeans')
        self.assertIn('3360', row['images'][0])
        self.assertNotIn('warehouseId', json.dumps(row['local_inventory']))
        self.assertEqual(row['publication_status'], 'active')
        self.assertNotIn('first_seen_at', row)

    def test_live_footwear_and_accessory_fixtures(self):
        for store in ('axel', 'quint'):
            scraper = AxelQuintScraper(store)
            self.addCleanup(scraper.close)
            source = json.loads((FIXTURES / f'{store}-shoes.json').read_text())
            rows = scraper.rows_from_snapshot(source)
            self.assertTrue(rows)
            self.assertTrue(all(row['category_path'] == ['Footwear', 'Shoes'] for row in rows))
            self.assertTrue(all(row['size_guide']['tables'] for row in rows))
        source = json.loads((FIXTURES / 'quint-accessory.json').read_text())
        self.assertEqual(scraper.rows_from_snapshot(source), [])

    def test_sale_formatted_fallback_and_distinct_colour_images(self):
        rows = self.scraper.rows_from_snapshot(fixture(index=2))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['current_price'] == 650 and r['list_price'] == 1300 for r in rows))
        self.assertEqual(len({r['source_color_id'] for r in rows}), 4)
        self.assertEqual(len({r['images'][0] for r in rows}), 4)
        self.assertEqual(rows[0]['materials'], ['100% Bomuld'])
        self.assertNotIn('Levering & returnering', rows[0]['description'])

    def test_quint_trouser_labels_and_zero_local_stock(self):
        scraper = AxelQuintScraper('quint')
        self.addCleanup(scraper.close)
        row = scraper.rows_from_snapshot(fixture('quint'))[0]
        self.assertEqual(row['aarhus_total_stock'], 0)
        self.assertEqual(row['local_total_stock'], 0)
        self.assertFalse(row['local_available'])
        self.assertIn('W30/32', row['local_inventory']['stores']['quint-bruuns-galleri']['sizes'])
        self.assertEqual(row['fit'], 'Loose fit')
        self.assertEqual(set(row['local_inventory']['stores']), {'quint-bruuns-galleri'})

    def test_non_aarhus_inventory_is_not_required_or_counted(self):
        for store in ('axel', 'quint'):
            with self.subTest(store=store):
                scraper = AxelQuintScraper(store)
                self.addCleanup(scraper.close)
                source = fixture(store)
                baseline = scraper.rows_from_snapshot(source)[0]
                for color in source['colors'].values():
                    for size in color['option']['sizes'].values():
                        size['stock'] = {key: value for key, value in size['stock'].items()
                                         if value['seoUrl'] in (scraper.config['aarhus'], 'online-shop')}
                row = scraper.rows_from_snapshot(source)[0]
                self.assertEqual(row['local_inventory'], baseline['local_inventory'])
                self.assertEqual(row['local_total_stock'], row['aarhus_total_stock'])
                self.assertEqual(row['local_available'], row['aarhus_available'])

    def test_unavailable_overrides_stale_stock_and_reactivation_restores_it(self):
        for name in ('axel', 'quint'):
            scraper = AxelQuintScraper(name)
            self.addCleanup(scraper.close)
            source = fixture(name)
            for color in source['colors'].values():
                color['option']['available'] = False
                for size in color['option']['sizes'].values():
                    size['availability'] = 5
                    for stock in size['stock'].values():
                        stock['stock'] = 5
            original = copy.deepcopy(source)
            for full in (True, False):
                rows = scraper.rows_from_snapshot(source, full=full)
                for row in rows:
                    self.assertEqual(row['aarhus_total_stock'], 0)
                    self.assertEqual(row['local_total_stock'], 0)
                    self.assertFalse(row['aarhus_available'])
                    self.assertFalse(row['local_available'])
                    for store in row['local_inventory']['stores'].values():
                        self.assertFalse(store['available'])
                        self.assertEqual(store['total_stock'], 0)
                        self.assertTrue(all(s == {'available': False, 'stock': 0} for s in store['sizes'].values()))
                    self.assertTrue(all(s['available'] is False and s['stock'] == 0 and s['online_warehouse_stock'] == 0 for s in row['webshop_sizes']))
                    if full:
                        self.assertEqual(row['raw']['source_option'], original['colors'][row['source_color_id']]['option'])
            self.assertEqual(source, original)
            for color in source['colors'].values():
                color['option']['available'] = True
            for row in scraper.rows_from_snapshot(source):
                self.assertEqual(row['publication_status'], 'active')
                self.assertTrue(row['aarhus_available'])
                self.assertGreater(row['aarhus_total_stock'], 0)

    def test_explicit_unavailable_and_missing_flag(self):
        for store in ('axel', 'quint'):
            scraper = AxelQuintScraper(store)
            self.addCleanup(scraper.close)
            source = fixture(store)
            for color in source['colors'].values():
                color['option']['available'] = False
            for full in (True, False):
                row = scraper.rows_from_snapshot(source, full=full)[0]
                self.assertEqual(row['publication_status'], 'unavailable')
                self.assertEqual(row['status_reason'], 'source_available_false')
                self.assertIsNotNone(row['discontinued_at'])
            for value in (None, 'false', 0):
                next(iter(source['colors'].values()))['option']['available'] = value
                with self.assertRaises(IncompleteProduct):
                    scraper.rows_from_snapshot(source)

    def test_missing_and_invalid_quantities_fail_closed(self):
        for mutation in ('missing_store', 'missing_quantity', 'negative', 'duplicate_size'):
            with self.subTest(mutation=mutation):
                source = fixture()
                sizes = next(iter(source['colors'].values()))['option']['sizes']
                first = next(iter(sizes.values()))
                key = next(k for k,v in first['stock'].items() if v['seoUrl']=='aarhus')
                if mutation=='missing_store': del first['stock'][key]
                elif mutation=='missing_quantity': del first['stock'][key]['stock']
                elif mutation=='negative': first['stock'][key]['stock'] = -1
                else: list(sizes.values())[1]['sizeName'] = first['sizeName']
                with self.assertRaises(IncompleteProduct): self.scraper.rows_from_snapshot(source)

    def test_currency_and_identity_required(self):
        for kind in ('currency','identity'):
            source = fixture()
            if kind=='identity': source['tracking']={}
            else:
                next(x for x in source['jsonld'] if x['@type']=='Product')['offers']['priceCurrency']='EUR'
            with self.assertRaises(IncompleteProduct): self.scraper.rows_from_snapshot(source)

    def test_accessory_exclusion_and_footwear(self):
        source=fixture()
        crumbs=next(x for x in source['jsonld'] if x['@type']=='BreadcrumbList')['itemListElement']
        crumbs[2]['name']='Accessories'
        self.assertEqual(self.scraper.rows_from_snapshot(source), [])
        crumbs[2]['name']='Sko & sneakers'
        self.assertEqual(self.scraper.rows_from_snapshot(source)[0]['category_path'], ['Footwear','Shoes'])

    def test_refresh_output_contains_only_identity_and_dynamic_fields(self):
        row=self.scraper.rows_from_snapshot(fixture(), full=False)[0]
        self.assertEqual(set(row), set(STORE_SPECS['axel'].dynamic_columns)|{'source_parent_id','source_color_id','publication_status','status_reason','status_checked_at','discontinued_at'})

    def test_size_guide_and_prices(self):
        self.assertEqual(formatted_price('DKK 1.300'),1300)
        self.assertEqual(formatted_price('199,60 kr.'),199.6)
        with self.assertRaises(IncompleteProduct): formatted_price('EUR 50')
        guide=parse_size_guide('<table><tr><th>Size</th><th>Waist</th></tr><tr><td>M</td><td>80 cm</td></tr></table>')
        self.assertEqual(guide['tables'][0][1],['M','80 cm'])

    def test_sitemap_follows_gzip_and_deduplicates(self):
        index=b'<sitemapindex><sitemap><loc>https://www.axel.dk/child.xml.gz</loc></sitemap></sitemapindex>'
        child=b'<urlset><url><loc>https://www.axel.dk/produkt/test?color=1</loc></url><url><loc>https://www.axel.dk/produkt/test</loc></url><url><loc>https://www.axel.dk/toej</loc></url></urlset>'
        self.scraper.session.get=Mock(side_effect=[Mock(content=index),Mock(content=gzip.compress(child))])
        self.assertEqual(self.scraper.discover_product_urls(),['https://www.axel.dk/produkt/test'])
        self.scraper.session.get=Mock(return_value=Mock(content=b'<urlset/>'))
        with self.assertRaises(IncompleteProduct): self.scraper.discover_product_urls()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        scraper=AxelQuintScraper('axel')
        self.rows=scraper.rows_from_snapshot(fixture())
        scraper.close()
        self.existing=[{**row,'id':100+i} for i,row in enumerate(self.rows)]
        self.args=Namespace(offset=0,limit=None,no_delay=True,max_retries=0,dry_run=False,no_history=False)
        self.database=Mock()
        self.database.list_rows.return_value=self.existing

    def run_refresh(self, rows=None, error=None):
        scraper=Mock()
        scraper.scrape.return_value=rows if rows is not None else self.rows
        scraper.scrape.side_effect=error
        with patch('scripts.refresh_axel_quint_inventory.AxelQuintScraper', return_value=scraper):
            return refresh_store('axel',self.database,self.args)

    def test_refresh_patches_database_id_and_skips_new_colours(self):
        extra={**self.rows[0],'source_color_id':'new'}
        stats=self.run_refresh(self.rows+[extra])
        self.assertEqual(stats.updated,1)
        spec,row_id,payload=self.database.patch_row.call_args.args
        self.assertEqual(row_id,100)
        self.assertEqual(set(payload),set(spec.dynamic_columns) | {'publication_status', 'status_reason', 'status_checked_at', 'discontinued_at'})
        self.database.queue_history_observation.assert_called_once()
        self.assertNotIn('raw',payload)

    def test_explicit_unavailable_is_written_even_when_every_colour_is_unavailable(self):
        source = fixture()
        for color in source['colors'].values():
            color['option']['available'] = False
        scraper = AxelQuintScraper('axel')
        self.addCleanup(scraper.close)
        rows = scraper.rows_from_snapshot(source, full=False)
        stats = self.run_refresh(rows)
        self.assertEqual(stats.unavailable, 1)
        self.assertEqual(stats.updated, 0)
        payload = self.database.patch_row.call_args.args[2]
        self.assertEqual(payload['publication_status'], 'unavailable')
        self.assertEqual(payload['aarhus_total_stock'], 0)
        self.assertEqual(self.database.queue_history_observation.call_args.args[0]['refresh_status'], 'source_unavailable')

    def test_all_missing_stops_before_any_write(self):
        with self.assertRaisesRegex(RuntimeError,'safety stop'):
            self.run_refresh(error=SourceUnavailable('404'))
        self.database.patch_row.assert_not_called()
        self.database.queue_history_observation.assert_not_called()

    def test_incomplete_and_wrong_parent_preserve_existing_rows(self):
        for mode in ('incomplete','wrong_parent'):
            with self.subTest(mode=mode):
                if mode=='incomplete': stats=self.run_refresh(error=IncompleteProduct('missing store'))
                else: stats=self.run_refresh([{**self.rows[0],'source_parent_id':'different'}])
                self.assertEqual(stats.failed,1)
                self.database.patch_row.assert_not_called()

    def test_dry_run_makes_no_database_writes(self):
        self.args.dry_run=True
        self.assertEqual(self.run_refresh().updated,1)
        self.database.patch_row.assert_not_called()
        self.database.queue_history_observation.assert_not_called()

    def test_catalogue_writes_final_batch_and_preserves_identity(self):
        args=parse_args('axel',['--url',self.rows[0]['canonical_url'],'--write-batch-size','50','--no-delay'])
        scraper=Mock()
        scraper.clean_url.side_effect=lambda x:x
        scraper.rows_from_snapshot.return_value=self.rows
        client=Mock()
        with patch('scripts.import_axel_quint_products.AxelQuintScraper',return_value=scraper), \
             patch('scripts.import_axel_quint_products.SupabaseCatalogClient',return_value=client), \
             patch('scripts.import_axel_quint_products.load_dotenv'), \
             patch('scripts.import_axel_quint_products.env',return_value='test'):
            self.assertEqual(import_products('axel',args),0)
        client.upsert_products.assert_called_once_with('axel_products',self.rows)

    def test_catalogue_failure_returns_nonzero_for_reconciliation(self):
        args=parse_args('axel',['--url',self.rows[0]['canonical_url'],'--dry-run'])
        scraper=Mock()
        scraper.clean_url.side_effect=lambda x:x
        scraper.fetch_snapshot.side_effect=IncompleteProduct('bad source')
        with patch('scripts.import_axel_quint_products.AxelQuintScraper',return_value=scraper), \
             patch('scripts.import_axel_quint_products.SupabaseCatalogClient') as client:
            self.assertEqual(import_products('axel',args),1)
            client.assert_not_called()


if __name__=='__main__': unittest.main()
