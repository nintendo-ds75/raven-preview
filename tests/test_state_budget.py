"""The inbox's polling budget, pinned. The prototype felt slow because the
inbox fetched, parsed and redrew the whole workspace every five seconds:
/api/state answers within 150 ms on the demo data; an unchanged poll is a
304 with no body; a change moves the ETag; a large body is gzip-compressed
for a client that accepts it; static files revalidate instead of being
downloaded on every visit; MCP responses are never compressed."""
import gzip
import json
import statistics
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import OfflineCase, ready_server
from bridge.store import Store


class StateBudgetTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'demo.db')
        self.store.seed()
        self.addCleanup(self.store.graph.close)
        server = ready_server(self.store, port=0)
        server.RequestHandlerClass.log_message = lambda *args: None
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f'http://127.0.0.1:{server.server_address[1]}'

    def get(self, path, **headers):
        try:
            with urlopen(Request(self.base + path, headers=headers)) as response:
                return response.status, dict(response.headers), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_state_answers_within_budget_on_demo_data(self):
        self.get('/api/state')
        samples = []
        for _ in range(7):
            started = time.perf_counter()
            status, headers, body = self.get('/api/state')
            samples.append((time.perf_counter() - started) * 1000)
        self.assertEqual(status, 200)
        self.assertLess(statistics.median(samples), 150, f'/api/state median {statistics.median(samples):.0f} ms')
        self.assertTrue(json.loads(body)['decisions'])

    def test_unchanged_state_is_a_304_and_a_change_moves_the_etag(self):
        status, headers, body = self.get('/api/state')
        tag = headers.get('ETag')
        self.assertTrue(tag and tag.startswith('W/"'), headers)
        status, headers, again = self.get('/api/state', **{'If-None-Match': tag})
        self.assertEqual((status, again), (304, b''))
        self.assertEqual(headers.get('ETag'), tag)
        pending = next(d for d in json.loads(body)['decisions'] if d['status'] == 'pending')
        self.store.answer(pending['id'], {'answer': 'Expire them at renewal.', 'rationale': 'Contract.'})
        status, headers, body = self.get('/api/state', **{'If-None-Match': tag})
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get('ETag'), tag)
        self.assertIn(pending['id'], body.decode())

    def test_trace_revalidates_too(self):
        run = json.loads(self.get('/api/state')[2])['runs'][0]['id']
        status, headers, _ = self.get(f'/api/tasks/{run}/trace')
        self.assertEqual(status, 200)
        self.assertEqual(self.get(f'/api/tasks/{run}/trace', **{'If-None-Match': headers['ETag']})[0], 304)

    def test_large_json_is_gzipped_only_for_a_client_that_accepts_it(self):
        status, plain_headers, plain = self.get('/api/state')
        self.assertIsNone(plain_headers.get('Content-Encoding'))
        status, headers, packed = self.get('/api/state', **{'Accept-Encoding': 'gzip, deflate'})
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertEqual(headers.get('Vary'), 'Accept-Encoding')
        self.assertLess(len(packed), len(plain) / 3)
        self.assertEqual(gzip.decompress(packed), plain)
        # The ETag names the content, not its encoding: a compressed poll revalidates.
        self.assertEqual(headers.get('ETag'), plain_headers.get('ETag'))
        status, headers, body = self.get('/api/state', **{'Accept-Encoding': 'gzip;q=0'})
        self.assertIsNone(headers.get('Content-Encoding'))
        status, headers, small = self.get('/api/me', **{'Accept-Encoding': 'gzip'})
        self.assertIsNone(headers.get('Content-Encoding'), 'a small body is not worth compressing')

    def test_static_files_revalidate_instead_of_reloading(self):
        for path in ('/', '/app.js', '/style.css'):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers.get('Cache-Control'), 'no-cache')
            self.assertEqual(self.get(path, **{'If-None-Match': headers['ETag']})[0], 304, path)
        status, headers, packed = self.get('/app.js', **{'Accept-Encoding': 'gzip'})
        self.assertEqual(headers.get('Content-Encoding'), 'gzip')
        self.assertIn(b'async function refresh', gzip.decompress(packed))
        self.assertEqual(self.get('/api/state')[1].get('Cache-Control'), 'no-store', 'the polled state is never cached')

    def test_mcp_responses_are_not_compressed(self):
        request = Request(self.base + '/mcp', method='POST', data=json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode(),
            headers={'Content-Type': 'application/json', 'Accept-Encoding': 'gzip', 'Accept': 'application/json, text/event-stream'})
        with urlopen(request) as response:
            self.assertIsNone(response.headers.get('Content-Encoding'))
            self.assertGreater(len(response.read()), 1024)
