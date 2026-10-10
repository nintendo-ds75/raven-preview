"""Documentation sites as a native context source, over real HTTP: the
workspace-shared attestation, llms.txt and sitemap discovery, robots.txt,
conditional requests, idempotent re-reads, changed and removed sections,
and a failed pass that keeps the cursor."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fixtures import OfflineCase
from bridge import source_docs, sources
from bridge.store import Invalid, Store

PAGE = """<html><head><title>Billing guide</title><script>track()</script></head><body>
<nav>Home | Docs</nav><h1>Billing guide</h1><p>Usage is metered monthly in UTC.</p>
<h2>Limits</h2><p>Overage is billed at 2x the plan rate.</p><pre>LIMIT = 500
CAP = 2</pre><footer>(c) Example</footer></body></html>"""


class Site:
    """A tiny site whose routes the test changes between passes."""

    def __init__(self):
        self.routes, self.requests = {}, []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                site.requests.append((self.path, dict(self.headers)))
                route = site.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, body, headers = route
                etag = headers.get('ETag')
                if etag and self.headers.get('If-None-Match') == etag:
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body.encode() if isinstance(body, str) else body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.origin = f'http://127.0.0.1:{self.server.server_port}'

    def page(self, path, body, etag='', content_type='text/html; charset=utf-8'):
        headers = {'Content-Type': content_type}
        if etag:
            headers['ETag'] = etag
        self.routes[path] = (200, body, headers)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class DocsSourceTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'docs.db')
        self.addCleanup(self.store.graph.close)
        self.site = Site()
        self.addCleanup(self.site.close)
        self.site.page('/llms.txt', '# Example\n\n- [Billing](/billing)\n- [Elsewhere](https://other.example/x)\n',
                       content_type='text/plain')
        self.site.page('/billing', PAGE, etag='"v1"')
        self.site.page('/robots.txt', 'User-agent: *\nDisallow: /private\n', content_type='text/plain')

    def add(self, **extra):
        return sources.register(self.store, 'docs', 'example/service', self.site.origin + '/', shared=True,
                                client=source_docs.HTTPClient(), **extra)

    def records(self):
        return self.store.graph.db.execute(
            "SELECT s.external_id, s.availability, v.snapshot FROM source_records s JOIN source_versions v "
            "ON v.id=s.head_id WHERE s.namespace=? ORDER BY s.external_id", ('docs:' + self.site.origin,)).fetchall()

    def test_registration_requires_the_workspace_shared_attestation(self):
        with self.assertRaisesRegex(Invalid, 'every reader'):
            sources.register(self.store, 'docs', 'example/service', self.site.origin + '/', shared=False,
                             client=source_docs.HTTPClient())
        self.assertEqual(sources.connections(self.store), [])

    def test_registration_refuses_non_https_remote_urls_and_credentials(self):
        for url in ('http://docs.example.com/', 'https://user:pw@docs.example.com/', 'ftp://docs.example.com/'):
            with self.assertRaises(Invalid):
                sources.register(self.store, 'docs', 'example/service', url, shared=True,
                                 client=source_docs.HTTPClient())

    def test_llms_txt_pages_are_split_at_headings_without_navigation_or_scripts(self):
        connection = self.add()
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['records'], 2)
        self.assertEqual(result['refused'], {'other_origin': 1})
        rows = self.records()
        bodies = [json.loads(r['snapshot'])['body'] for r in rows]
        joined = '\n'.join(bodies)
        self.assertIn('Usage is metered monthly in UTC.', joined)
        self.assertIn('LIMIT = 500\nCAP = 2', joined)  # code kept whole
        for noise in ('track()', 'Home | Docs', '(c) Example'):
            self.assertNotIn(noise, joined)
        self.assertTrue(all(r['availability'] == 'available' for r in rows))
        self.assertTrue(rows[0]['external_id'].startswith(self.site.origin + '/billing#section-'))

    def test_a_second_pass_sends_validators_and_writes_nothing_new(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        versions = self.store.graph.db.execute('SELECT count(*) n FROM source_versions').fetchone()['n']
        again = sources.sync(self.store, connection['id'])
        self.assertEqual(again['records'], 0)
        billing = [h for p, h in self.site.requests if p == '/billing']
        self.assertEqual(billing[-1].get('If-None-Match'), '"v1"')
        self.assertEqual(self.store.graph.db.execute('SELECT count(*) n FROM source_versions').fetchone()['n'], versions)

    def test_a_changed_section_is_a_new_version_and_a_removed_one_is_marked_deleted(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.site.page('/billing', '<html><head><title>Billing guide</title></head><body><h1>Billing guide</h1>'
                                   '<p>Usage is metered weekly.</p></body></html>', etag='"v2"')
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['changed'], 2)
        states = {r['external_id'].rsplit('-', 1)[1]: r['availability'] for r in self.records()}
        self.assertEqual(states, {'0': 'available', '1': 'deleted'})
        first = json.loads(self.records()[0]['snapshot'])['body']
        self.assertIn('weekly', first)

    def test_a_page_that_disappears_is_marked_deleted_not_erased(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        del self.site.routes['/billing']
        sources.sync(self.store, connection['id'])
        self.assertEqual({r['availability'] for r in self.records()}, {'deleted'})
        self.assertEqual(len(self.records()), 2)

    def test_robots_and_page_limits_are_counted_as_refusals(self):
        self.site.page('/llms.txt', '- [A](/billing)\n- [B](/private/plan)\n- [C](/two)\n', content_type='text/plain')
        self.site.page('/two', '<h1>Two</h1><p>Second page.</p>')
        connection = self.add(options={'max_pages': 1})
        result = sources.sync(self.store, connection['id'])
        self.assertEqual(result['refused'], {'robots_disallowed': 1, 'over_page_limit': 1})
        self.assertFalse(any(p.startswith('/private') for p, _ in self.site.requests))

    def test_sitemap_is_used_when_there_is_no_llms_txt(self):
        del self.site.routes['/llms.txt']
        self.site.page('/sitemap.xml', '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                                       f'<url><loc>{self.site.origin}/billing</loc></url></urlset>',
                       content_type='application/xml')
        connection = self.add()
        sources.sync(self.store, connection['id'])
        self.assertEqual(sources._row(self.store, connection['id'])['cursor'], 'sitemap.xml')
        self.assertEqual(len(self.records()), 2)

    def test_a_failed_pass_records_the_error_and_keeps_the_previous_state(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        before = sources._row(self.store, connection['id'])
        self.site.routes['/billing'] = (500, 'boom', {})
        with self.assertRaises(sources.SourceError):
            sources.sync(self.store, connection['id'])
        after = sources._row(self.store, connection['id'])
        self.assertIn('HTTP 500', after['last_error'])
        self.assertEqual((after['cursor'], after['last_success_at']), (before['cursor'], before['last_success_at']))
        self.assertEqual({r['availability'] for r in self.records()}, {'available'})
        report = sources.report(self.store)['sources'][0]
        self.assertEqual(report['state'], 'needs_attention')
        self.assertTrue(any('HTTP 500' in g for g in report['limits']))
        self.assertTrue(any('docs source' in r['what'] for r in sources.readiness(self.store)))

    def test_a_disabled_source_is_not_synced_and_keeps_its_records(self):
        connection = self.add()
        sources.sync(self.store, connection['id'])
        sources.disable(self.store, connection['id'], by='admin')
        with self.assertRaisesRegex(Invalid, 'disabled'):
            sources.sync(self.store, connection['id'])
        self.assertEqual(sources.SourceSyncer(self.store, clients={'docs': source_docs.HTTPClient()}).tick(), [])
        self.assertEqual(len(self.records()), 2)

    def test_redirects_off_the_registered_origin_are_refused(self):
        self.site.routes['/billing'] = (302, '', {'Location': 'https://other.example/billing'})
        connection = self.add()
        with self.assertRaisesRegex(sources.SourceError, 'HTTP 302'):
            sources.sync(self.store, connection['id'])
