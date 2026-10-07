from __future__ import annotations
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scrapers.full_import.salling import SallingScraper, decode_nuxt
from scrapers.full_import.magasin import MagasinScraper, parse_stock
from scrapers.full_import.department_stores import classification, SourceUnavailable
from scripts.refresh_department_inventory import refresh_store
from scripts.refresh_store_inventory import parse_args as refresh_args, STORE_SPECS
from scripts.import_axel_quint_products import import_products, parse_args

FIXTURES = Path(__file__).parent / 'fixtures' / 'department_stores'


def fixture(store, kind='regular'):
    return json.loads((FIXTURES / f'{store}-{kind}.json').read_text())


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.salling = SallingScraper()
        self.magasin = MagasinScraper()
        self.addCleanup(self.salling.close)
        self.addCleanup(self.magasin.close)

    def test_salling_one_colour_row_not_one_size_row(self):
        rows = self.salling.rows_from_snapshot(fixture('salling', 'sale'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r['source_color_id'] for r in rows}), 2)
        for row in rows:
            self.assertEqual(row['current_price'],156)
            self.assertEqual(row['list_price'],260)
            self.assertEqual(row['name'], 'Base T-shirt')
            self.assertEqual(row['category_path'], ['Clothing','T-shirts'])
            self.assertGreater(len(row['webshop_sizes']),1)
            self.assertNotIn('width=64', row['images'][0])
            self.assertNotIn('publication_status', row)
            self.assertNotIn('first_seen_at', row)

    def test_salling_exact_stock_and_unknown_disabled_combinations(self):
        rows = self.salling.rows_from_snapshot(fixture('salling'))
        self.assertEqual(len(rows),2)
        by_color = {r['source_color_id']:r for r in rows}
        mixed = by_color['black/white/black/white/grey heather']
        store = mixed['local_inventory']['stores']['salling-aarhus']
        self.assertIsNone(store['sizes']['XS']['available'])
        self.assertIsNone(store['sizes']['XS']['stock'])
        self.assertEqual(store['sizes']['L']['stock'],9)
        self.assertIsNone(mixed['aarhus_total_stock'])
        self.assertTrue(mixed['aarhus_available'])
        self.assertFalse(store['stock_known'])
        for row in rows:
            self.assertEqual(set(row['local_inventory']['stores']),{'salling-aarhus'})
            self.assertEqual(row['aarhus_total_stock'],row['local_total_stock'])

    def test_missing_local_stock_cannot_be_treated_as_zero(self):
        source=fixture('salling')
        next(p for p in source['products'] if not p.get('_source_page_unavailable'))['indexableStockInfo']=[]
        with self.assertRaisesRegex(ValueError,'Aarhus stock'):
            self.salling.rows_from_snapshot(source)

    def test_salling_negative_fractional_and_null_stock_rejected(self):
        for bad in (-1,.5,None,True):
            with self.subTest(bad=bad):
                source=fixture('salling')
                p=next(p for p in source['products'] if not p.get('_source_page_unavailable'))
                next(s for s in p['indexableStockInfo'] if s['name']=='Salling Aarhus')['availableStock']=bad
                with self.assertRaises(ValueError):self.salling.rows_from_snapshot(source)

    def test_incomplete_size_enumeration_rejected(self):
        source=fixture('salling','sale');source['products'].pop()
        with self.assertRaisesRegex(ValueError,'size coverage'):
            self.salling.rows_from_snapshot(source)

    def test_magasin_sale_prices_and_boolean_local_inventory(self):
        row=self.magasin.rows_from_snapshot(fixture('magasin','sale'))[0]
        self.assertEqual(row['current_price'],279.3)
        self.assertEqual(row['list_price'],399)
        self.assertIsNone(row['aarhus_total_stock'])
        self.assertEqual(set(row['local_inventory']['stores']),{'magasin-aarhus'})
        store=row['local_inventory']['stores']['magasin-aarhus']
        self.assertFalse(store['stock_known'])
        self.assertTrue(all(s['stock'] is None for s in store['sizes'].values()))
        rows=self.magasin.rows_from_snapshot(fixture('magasin'))
        self.assertEqual(len(rows),3)
        self.assertTrue(all(r['current_price']==899 and r['list_price'] is None for r in rows))
        self.assertTrue(rows[0]['size_guide']['tables'])
        self.assertIsNone(rows[0]['description']) # A colour name is not a description.
        self.assertEqual(rows[0]['color_group'],'Blå')
        self.assertEqual(rows[0]['category_path'],['Clothing','Shirts'])

    def test_stock_negative_message_wins_and_missing_is_error(self):
        html=(FIXTURES/'magasin-stock.html').read_text()
        self.assertEqual(parse_stock(html,'S15650303'),{'available':False,'stock':None})
        self.assertTrue(parse_stock(html.replace('Ikke på lager','På lager'),'S15650303')['available'])
        for bad in (html.replace('Magasin Aarhus','Another shop'),html.replace('Ikke på lager','Ukendt'),'<html>Error</html>'):
            with self.assertRaises(ValueError):parse_stock(bad,'S15650303')
        with self.assertRaises(ValueError):parse_stock(html,'wrong-size')

    def test_stock_and_online_availability_are_separate(self):
        source=fixture('magasin')
        p=source['colors'][0]['product']
        for a in p['variationAttributes']:
            if a['id']=='size':
                for v in a['values']:v['inStock']=False
        row=self.magasin.rows_from_snapshot(source)[0]
        self.assertFalse(any(s['available'] for s in row['webshop_sizes']))
        self.assertTrue(row['aarhus_available'])

    def test_dynamic_rows_have_no_stable_metadata(self):
        for store,scraper in [('salling',self.salling),('magasin',self.magasin)]:
            rows=scraper.rows_from_snapshot(fixture(store),full=False)
            allowed={'source_parent_id','source_color_id',*STORE_SPECS[store].dynamic_columns}
            for row in rows:self.assertFalse(set(row)-allowed)

    def test_non_clothing_and_wrong_gender_excluded(self):
        for path,name in [(['Dame','Tøj'],'Skjorte'),(['Herre','Tøj','Jakker'],'Thornproof Dressing Voks'),(['Herre','Sko','Skopleje'],'Børste')]:
            self.assertIsNone(classification(path,name))
        self.assertEqual(classification(['Herre','Sko','Sneakers']),'Footwear')

    def test_nuxt_shared_refs_and_fail_closed(self):
        arr=[['Reactive',1],{'a':2,'b':2,'empty':3},'hello',['EmptyRef']]
        parsed=decode_nuxt('<script id="__NUXT_DATA__">'+json.dumps(arr)+'</script>')
        self.assertEqual(parsed,{'a':'hello','b':'hello','empty':None})
        with self.assertRaises(ValueError):decode_nuxt('<html>Unavailable</html>')


class RunnerTests(unittest.TestCase):
    def setUp(self):
        s=MagasinScraper();self.rows=s.rows_from_snapshot(fixture('magasin'));s.close()
        self.existing=[dict(r,id=i+1,publication_status='active') for i,r in enumerate(self.rows[:2])]
        self.database=Mock();self.database.list_rows.return_value=self.existing
        self.args=refresh_args(['--store','magasin','--no-delay'])
        self.scraper=Mock();self.scraper.scrape.return_value=self.rows

    def test_refresh_groups_style_patches_ids_and_skips_new_colours(self):
        with patch('scripts.refresh_department_inventory.make_scraper',return_value=self.scraper):
            stats=refresh_store('magasin',self.database,self.args)
        self.assertEqual(stats.updated,2);self.assertEqual(stats.pages,1)
        self.scraper.scrape.assert_called_once()
        self.assertEqual([c.args[1] for c in self.database.patch_row.call_args_list],[1,2])
        for c in self.database.patch_row.call_args_list:
            self.assertNotIn('name',c.args[2]);self.assertNotIn('raw',c.args[2])

    def test_failed_stock_or_missing_style_never_zeroes_inventory(self):
        for error in (ValueError('stock missing'),SourceUnavailable('404')):
            with self.subTest(error=error):
                self.database.reset_mock();self.scraper.scrape.side_effect=error
                with patch('scripts.refresh_department_inventory.make_scraper',return_value=self.scraper):
                    stats=refresh_store('magasin',self.database,self.args)
                self.assertEqual(stats.failed,2);self.database.patch_row.assert_not_called()
                self.database.queue_history_observation.assert_not_called()

    def test_missing_colour_does_not_partially_write_style(self):
        self.scraper.scrape.return_value=self.rows[:1]
        with patch('scripts.refresh_department_inventory.make_scraper',return_value=self.scraper):
            stats=refresh_store('magasin',self.database,self.args)
        self.assertEqual(stats.failed,2);self.database.patch_row.assert_not_called()

    def test_dry_run_and_last_import_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            args=parse_args('magasin',['--url',self.rows[0]['canonical_url'],'--dry-run','--output',tmp+'/rows.json'])
            scraper=Mock();scraper.covered_urls=set();scraper.clean_url.side_effect=lambda x:x
            scraper.rows_from_snapshot.return_value=self.rows
            with patch('scripts.import_axel_quint_products.SupabaseCatalogClient') as client:
                self.assertEqual(import_products('magasin',args,lambda *a,**kw:scraper),0)
                client.assert_not_called()
            args.dry_run=False;args.output=None
            with patch('scripts.import_axel_quint_products.SupabaseCatalogClient') as cls,patch('scripts.import_axel_quint_products.load_dotenv'),patch('scripts.import_axel_quint_products.env',return_value='fake'):
                self.assertEqual(import_products('magasin',args,lambda *a,**kw:scraper),0)
                cls.return_value.upsert_products.assert_called_once_with('magasin_products',self.rows)

    def test_database_failure_propagates_and_stops_the_refresh(self):
        from scripts.refresh_store_inventory import SupabasePatchError
        self.database.patch_row.side_effect=SupabasePatchError('write failed')
        with patch('scripts.refresh_department_inventory.make_scraper',return_value=self.scraper):
            with self.assertRaises(SupabasePatchError):refresh_store('magasin',self.database,self.args)
        self.assertEqual(self.database.patch_row.call_count,1)
        self.database.queue_history_observation.assert_not_called()
        self.scraper.close.assert_called_once()

    def test_input_requires_single_store_and_dry_run(self):
        for argv in (['--store','salling','--input','x'],['--all','--dry-run','--input','x']):
            with self.assertRaises(SystemExit):refresh_args(argv)
        self.assertEqual(refresh_args(['--store','salling','--dry-run','--input','x']).input,'x')


class DiscoveryAndSourceTests(unittest.TestCase):
    def test_salling_disabled_size_does_not_copy_another_colour_stock(self):
        source=fixture('salling')
        available={str(p['id']):p for p in source['products'] if not p.get('_source_page_unavailable')}
        selected=next(p for p in available.values() if p['id']==1061715)
        scraper=SallingScraper();self.addCleanup(scraper.close)
        def fetch(url):
            import re
            pid=re.search(r'/p-(\d+)/?$',url)[1]
            if pid not in available:raise SourceUnavailable('disabled size')
            return copy.deepcopy(available[pid])
        with patch.object(scraper,'fetch_product',side_effect=fetch):
            rows=scraper.scrape('https://salling.dk'+selected['url'])
        row=next(r for r in rows if r['source_color_id'].startswith('black/white'))
        self.assertEqual(row['current_price'],599) # Disabled option's stale 419.30 is ignored.
        self.assertEqual(row['name'],'Icon Cotton Stretch 5-pak Boxer Briefs')
        self.assertIsNone(row['local_inventory']['stores']['salling-aarhus']['sizes']['XS']['available'])

    def test_magasin_pagination_requires_a_complete_catalog(self):
        scraper=MagasinScraper();self.addCleanup(scraper.close)
        def page(n,shown,total,next_page=None,pid='A'):
            links=f'<div class="productTile"><a class="b-productcard__media-link" href="/shoe/{pid}.html"></a></div>'
            more=f'<div class="load-more" data-callurl="https://www.magasin.dk/internalsearchpages/?page={next_page}"></div>' if next_page else ''
            return links+f'<div class="grid-footer" data-page-number="{n-1}" data-page-size="1">Viser {shown} ud af {total}{more}</div>'
        replies=[Mock(text=page(1,1,2,2,'A')),Mock(text=page(2,2,2,None,'B')),Mock(text=page(1,1,1,None,'C'))]
        with patch.object(scraper,'get',side_effect=replies):
            self.assertEqual(len(scraper.discover_product_urls()),3)
        with patch.object(scraper,'get',return_value=Mock(text=page(1,1,2,None))):
            with self.assertRaisesRegex(ValueError,'before catalog end'):scraper.discover_product_urls()

    def test_magasin_accepts_live_empty_terminal_placeholder_but_not_empty_middle(self):
        scraper=MagasinScraper();self.addCleanup(scraper.close)
        terminal=(FIXTURES/'magasin-final-page.html').read_text()
        def full_page(n):
            return (f'<div class="productTile"><a class="b-productcard__media-link" href="/item/P{n}.html"></a></div>'
                    f'<div class="grid-footer" data-page-number="{n-1}" data-page-size="36">Viser {n*36} ud af 1189'
                    f'<div class="load-more" data-callurl="https://www.magasin.dk/internalsearchpages/?page={n+1}"></div></div>')
        responses=[Mock(text=full_page(n)) for n in range(1,34)]+[Mock(text=terminal)]
        with patch.object(scraper,'get',side_effect=responses*2):
            self.assertEqual(len(scraper.discover_product_urls()),33)
        middle='<div class="grid-footer" data-page-number="1" data-page-size="36">Viser 72 ud af 1189</div>'
        with patch.object(scraper,'get',side_effect=[Mock(text=full_page(1)),Mock(text=middle)]):
            with self.assertRaisesRegex(ValueError,'Empty or repeated'):scraper.discover_product_urls()

    def test_salling_discovery_deduplicates_size_pages_by_parent(self):
        scraper=SallingScraper();self.addCleanup(scraper.close)
        def payload(page,pages,items):
            return {'data':{'x':{'content':{'content':{'categoryDetails':{'data':{'productList':{
                'page':page,'products':{'totalPages':pages,'results':items}}}}}}}}}
        one={'id':1,'parentId':10,'link':'/herre/toej/tee/p-1/'}
        two={'id':2,'parentId':10,'link':'/herre/toej/tee/p-2/'}
        shoe={'id':3,'parentId':11,'link':'/herre/sko/shoe/p-3/'}
        with patch.object(scraper,'get',return_value=Mock(text='html')),patch('scrapers.full_import.salling.decode_nuxt',side_effect=[payload(1,2,[one]),payload(2,2,[two]),payload(1,1,[shoe])]):
            self.assertEqual(set(scraper.discover_product_urls()),{'https://salling.dk'+one['link'],'https://salling.dk'+shoe['link']})

    def test_explicitly_disabled_magasin_stock_stays_unknown(self):
        source=fixture('magasin')
        products={c['product']['id']:c['product'] for c in source['colors']}
        for p in products.values():p['showStockAvailabilityOption']=False
        scraper=MagasinScraper();self.addCleanup(scraper.close)
        with patch.object(scraper,'product',side_effect=lambda pid:copy.deepcopy(products[pid])),patch.object(scraper,'get') as get:
            rows=scraper.scrape('https://www.magasin.dk/check-flannel-shirt/BRYA69-00H5.html',full=False)
            get.assert_not_called()
        self.assertTrue(all(r['aarhus_available'] is None for r in rows))

    def test_complete_salling_stock_totals_are_exact(self):
        source=fixture('salling','sale')
        for p in source['products']:
            p.pop('_source_page_unavailable',None)
            p['indexableStockInfo']=[{'name':'Salling Aarhus','availableStock':2},{'name':'Salling Aalborg','availableStock':999}]
        scraper=SallingScraper();self.addCleanup(scraper.close)
        for row in scraper.rows_from_snapshot(source):
            self.assertEqual(row['aarhus_total_stock'],2*len(row['webshop_sizes']))
            self.assertTrue(row['local_inventory']['stores']['salling-aarhus']['stock_known'])

    def test_new_sunday_jobs_use_the_registered_importers_without_db_in_dry_run(self):
        from scripts.sync_store_catalogs import parse_args as sync_args, sync_catalogs, importer_command
        for store in ('salling','magasin'):
            with patch('sys.argv',['sync', '--store',store,'--dry-run','--limit','1']):args=sync_args()
            captured=[]
            def importer(spec,args):
                captured.append(importer_command(spec,args));return 0
            with patch('scripts.sync_store_catalogs.run_importer',side_effect=importer),patch('scripts.sync_store_catalogs.CatalogLifecycleClient') as client,patch('scripts.sync_store_catalogs.CatalogSyncRunRecorder') as recorder,patch('scripts.sync_store_catalogs.load_dotenv'):
                self.assertEqual(sync_catalogs(args),0)
                client.assert_not_called();recorder.assert_not_called()
            self.assertTrue(captured[0][1].endswith(f'import_{store}_products.py'))
            self.assertIn('--dry-run',captured[0])


if __name__=='__main__':unittest.main()
