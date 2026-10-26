import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import signal
import requests

from scripts import import_axel_quint_products as imports
from scripts import run_department_import_batches as runner
from scrapers.source_http import SourcePayloadError


class CheckpointTests(unittest.TestCase):
    def test_empty_response_is_deferred_but_repeated_empty_responses_stop_store(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.fetch_snapshot.side_effect = [SourcePayloadError('Empty HTTP 200'), {}]
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 1)
            state = json.loads(path.read_text())
            self.assertEqual(state['completed'], ['https://example.com/2'])
            self.assertEqual(state['last_batch']['status'], 'partial')
            self.assertTrue(runner.batch_can_continue({}, state, 1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.discover_product_urls.return_value = [f'https://example.com/{n}' for n in range(26)]
            scraper.fetch_snapshot.side_effect = SourcePayloadError('Empty HTTP 200')
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=26), 1)
            self.assertEqual(scraper.fetch_snapshot.call_count, 25)
            self.assertEqual(json.loads(path.read_text())['last_batch']['status'], 'blocked')

    def server_error(self, code=500):
        response = requests.Response()
        response.status_code = code
        return requests.HTTPError('Source server error', response=response)

    def test_isolated_server_error_is_deferred_and_next_product_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.fetch_snapshot.side_effect = [self.server_error(), {}]
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 1)
            state = json.loads(path.read_text())
            self.assertEqual(state['completed'], ['https://example.com/2'])
            self.assertEqual(state['deferred'], ['https://example.com/1'])
            self.assertEqual(state['last_batch']['status'], 'partial')
            self.assertTrue(runner.batch_can_continue({}, state, 1))

    def test_25_consecutive_server_errors_stop_without_visiting_26th_page(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.discover_product_urls.return_value = [f'https://example.com/{n}' for n in range(26)]
            scraper.fetch_snapshot.side_effect = self.server_error(503)
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=26), 1)
            self.assertEqual(scraper.fetch_snapshot.call_count, 25)
            state = json.loads(path.read_text())
            self.assertEqual(state['last_batch']['status'], 'blocked')
            self.assertEqual(len(state['deferred']), 24)
            self.assertFalse(runner.batch_can_continue({}, state, 1))

    def test_runner_processes_next_batch_after_partial_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            state = {'version': 1, 'store': 'salling', 'urls': ['one', 'two', 'three'], 'completed': []}
            path.write_text(json.dumps(state))
            outcomes = iter([(['one'], ['two'], 'partial', 1),
                             (['one', 'three'], ['two'], 'success', 0)])
            def run(command, timeout):
                done, deferred, status, result = next(outcomes)
                state.update(completed=done, deferred=deferred,
                             last_batch={'id': str(len(done)), 'status': status, 'failed_pages': len(deferred)})
                path.write_text(json.dumps(state))
                return result
            with patch('sys.argv', ['runner', '--store', 'salling', '--checkpoint', str(path),
                                   '--lock', str(Path(directory) / 'lock')]), \
                    patch.object(runner, 'run_batch', side_effect=run) as batches, \
                    patch.object(runner.time, 'sleep'):
                self.assertEqual(runner.main(), 1)  # Incomplete pass, not false success.
                self.assertEqual(batches.call_count, 2)

    def test_transport_error_stops_without_deferring_all_remaining_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.fetch_snapshot.side_effect = requests.ConnectionError('Source offline')
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 1)
            self.assertEqual(scraper.fetch_snapshot.call_count, 1)
            state = json.loads(path.read_text())
            self.assertEqual(state['last_batch']['status'], 'blocked')
            self.assertEqual(state['deferred'], [])

    def test_page_errors_are_deferred_and_successful_family_clears_its_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            scraper.fetch_snapshot.side_effect = [ValueError('Missing price'), {}]
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 1)
            state = json.loads(path.read_text())
            self.assertEqual(state['deferred'], ['https://example.com/1'])
            self.assertEqual(state['failures']['https://example.com/1']['error'], 'Missing price')
            self.assertEqual(state['last_batch']['status'], 'partial')
            scraper.reset_mock()
            scraper.fetch_snapshot.side_effect = None
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 0)
            scraper.fetch_snapshot.assert_not_called()
            args = imports.parse_args('salling', ['--checkpoint', str(path), '--retry-failed', '--no-delay'])
            with patch.object(imports, 'load_dotenv'), patch.object(imports, 'env', return_value='fake'), \
                    patch.object(imports, 'SupabaseCatalogClient', return_value=Mock()):
                self.assertEqual(imports.import_products('salling', args, lambda *a, **kw: scraper), 0)
            self.assertEqual(json.loads(path.read_text())['failures'], {})

    def test_runner_continues_partial_progress_but_not_blocked_stale_or_db_failures(self):
        before = {'completed': [], 'deferred': [], 'last_batch': {'id': 'old'}}
        after = {'completed': ['one'], 'deferred': ['two'],
                 'last_batch': {'id': 'new', 'status': 'partial'}}
        self.assertTrue(runner.batch_can_continue(before, after, 1))
        for status in ('blocked', 'running'):
            after['last_batch']['status'] = status
            self.assertFalse(runner.batch_can_continue(before, after, 1))
        after['last_batch'] = {'id': 'old', 'status': 'partial'}
        self.assertFalse(runner.batch_can_continue(before, after, 1))
        after['last_batch'] = {'id': 'new', 'status': 'partial'}
        after['completed'], after['deferred'] = [], []
        self.assertFalse(runner.batch_can_continue(before, after, 1))

    def test_ctrl_c_saves_previous_complete_family_and_resume_skips_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            database, scraper = Mock(), self.scraper()
            scraper.fetch_snapshot.side_effect = [{}, KeyboardInterrupt()]
            self.assertEqual(self.run_batch(path, database, scraper, pages=2), 130)
            database.upsert_products.assert_called_once()
            self.assertEqual(json.loads(path.read_text())['completed'], ['https://example.com/1'])
            scraper.reset_mock()
            scraper.fetch_snapshot.side_effect = None
            self.assertEqual(self.run_batch(path, database, scraper), 0)
            scraper.fetch_snapshot.assert_called_once_with('https://example.com/2')

    def test_runner_waits_for_child_flush_on_ctrl_c(self):
        process = Mock()
        process.wait.side_effect = [KeyboardInterrupt(), 130]
        with patch.object(runner.subprocess, 'Popen', return_value=process):
            self.assertEqual(runner.run_batch(['importer'], 100), 130)
        process.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(process.wait.call_args_list[-1].kwargs['timeout'], 90)
        process.kill.assert_not_called()

    def test_validated_colours_outside_batch_are_checkpointed_after_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            def snapshot(url):
                scraper.covered_urls.update(scraper.discover_product_urls.return_value)
                return {}
            scraper.fetch_snapshot.side_effect = snapshot
            self.assertEqual(self.run_batch(path, Mock(), scraper), 0)
            self.assertEqual(len(json.loads(path.read_text())['completed']), 2)
            scraper.reset_mock()
            self.assertEqual(self.run_batch(path, Mock(), scraper), 0)
            scraper.fetch_snapshot.assert_not_called()

    def test_old_log_seed_excludes_uncommitted_and_last_split_family(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.log'
            path.write_text('INFO Running weekly full import for salling:\n'
                ' INFO salling [1/4] Hydrating https://example.com/1\n'
                ' INFO salling hydrated 2 colour rows\n'
                ' INFO salling [2/4] Hydrating https://example.com/2\n'
                ' INFO salling hydrated 3 colour rows\n'
                ' INFO Upserted 50 salling colour rows.\n'
                ' INFO salling [3/4] Hydrating https://example.com/3\n'
                ' ERROR Failed salling product\n'
                ' INFO salling [4/4] Hydrating https://example.com/4\n'
                ' INFO salling hydrated 1 colour rows\n')
            self.assertEqual(imports.committed_salling_pages(path), {'https://example.com/1'})

    def run_batch(self, path, database, scraper, pages=1):
        args = imports.parse_args('salling', ['--checkpoint', str(path), '--batch-pages', str(pages), '--no-delay'])
        with patch.object(imports, 'load_dotenv'), patch.object(imports, 'env', return_value='fake'), \
                patch.object(imports, 'SupabaseCatalogClient', return_value=database):
            return imports.import_products('salling', args, lambda *a, **kw: scraper)

    def scraper(self):
        scraper = Mock()
        scraper.covered_urls = set()
        scraper.clean_url.side_effect = lambda url: url
        scraper.discover_product_urls.return_value = ['https://example.com/1', 'https://example.com/2']
        scraper.rows_from_snapshot.return_value = [{'source_parent_id': 'one', 'source_color_id': 'blue'}]
        return scraper

    def test_restart_uses_manifest_and_skips_only_acknowledged_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            database, scraper = Mock(), self.scraper()
            self.assertEqual(self.run_batch(path, database, scraper), 0)
            self.assertEqual(json.loads(path.read_text())['completed'], ['https://example.com/1'])
            scraper.reset_mock()
            self.assertEqual(self.run_batch(path, database, scraper), 0)
            scraper.discover_product_urls.assert_not_called()
            scraper.fetch_snapshot.assert_called_once_with('https://example.com/2')
            self.assertEqual(len(json.loads(path.read_text())['completed']), 2)

    def test_failed_write_does_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            database = Mock()
            database.upsert_products.side_effect = RuntimeError('DB unavailable')
            self.assertEqual(self.run_batch(path, database, self.scraper()), 1)
            self.assertEqual(json.loads(path.read_text())['completed'], [])

    def test_failed_family_is_not_skipped_via_covered_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'checkpoint.json'
            scraper = self.scraper()
            def snapshot(url):
                scraper.covered_urls.add('https://example.com/2')
                return {}
            scraper.fetch_snapshot.side_effect = snapshot
            scraper.rows_from_snapshot.side_effect = [ValueError('Incomplete family'),
                [{'source_parent_id': 'two', 'source_color_id': 'blue'}]]
            self.assertEqual(self.run_batch(path, Mock(), scraper, pages=2), 1)
            self.assertEqual(scraper.fetch_snapshot.call_count, 2)
            self.assertEqual(json.loads(path.read_text())['completed'], ['https://example.com/2'])


if __name__ == '__main__':
    unittest.main()
