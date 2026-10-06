"""Synthetic, provider-free external record state and snapshot contracts."""
import json
import threading
from pathlib import Path
from urllib.request import Request, urlopen

from fixtures import OfflineCase, ready_server
from bridge import mcp
from bridge.ladder import _is_settled
from bridge.store import Invalid, Store


class RecordStateTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'records.db')
        self.addCleanup(self.store.graph.close)
        self.base = dict(repo='synthetic/records', kind='jira', ref='POL-1', title='Retention policy')

    def row(self, ref='POL-1'):
        return dict(self.store.graph.intents_by_ref([ref], self.base['repo'])[0])

    def test_open_or_unknown_state_never_settled_for_any_import_kind(self):
        for kind in ('ticket', 'jira', 'issue', 'doc', 'slack', 'note'):
            for state in ('Open', 'In Progress', 'Pending', 'Draft', 'Cancelled', 'Rejected', 'Superseded', 'Needs custom review'):
                for supplied in ({}, {'resolved': False}, {'resolved': True}):
                    with self.subTest(kind=kind, state=state, supplied=supplied):
                        self.store.add_record({**self.base, 'kind': kind, 'status': state, **supplied})
                        row = next(dict(r) for r in self.store.graph.intents_by_ref(['POL-1'], self.base['repo']) if r['kind'] == kind)
                        self.assertFalse(_is_settled(row))

    def test_false_normalization_and_closed_are_evidence_only(self):
        for value in (False, 0, '0', 'false', 'FALSE', ' no ', 'No'):
            self.store.add_record({**self.base, 'status': 'Done', 'resolved': value})
            self.assertFalse(_is_settled(self.row()))
        self.store.add_record({**self.base, 'status': 'Done'})
        self.assertTrue(_is_settled(self.row()))
        for table in ('decisions', 'authority'):
            self.assertEqual(self.store.graph.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        with self.assertRaises(Invalid):
            self.store.add_record({**self.base, 'resolved': 'maybe'})

    def test_paths_replace_clear_and_omit_with_versioned_metadata(self):
        self.store.add_record({**self.base, 'paths': ['old/a.py'], 'author': 'Original', 'created_at': '2025-01-01'})
        first = self.row()
        self.store.add_record({**self.base, 'paths': ['new/b.py'], 'author': 'Later', 'created_at': '2026-01-01'})
        second = self.row()
        self.assertEqual([second[k] for k in ('id', 'repo', 'kind', 'ref')],
                         [first[k] for k in ('id', 'repo', 'kind', 'ref')])
        self.assertEqual((second['author'], second['created_at']), ('Later', '2026-01-01'))
        versions = self.store.get_record(second['record_id'], self.base['repo'])['versions']
        self.assertEqual(versions[0]['snapshot']['author'], 'Original')
        self.assertEqual(versions[0]['snapshot']['created_at'], '2025-01-01')
        def paths():
            return self.store.graph.paths_of_intents(self.base['repo'], [('jira', 'POL-1')]).get(('jira', 'POL-1'), [])
        self.assertEqual(paths(), ['new/b.py'])
        self.store.add_record(self.base)
        self.assertEqual(paths(), ['new/b.py'])
        self.store.add_record({**self.base, 'paths': []})
        self.assertEqual(paths(), [])

    def test_mcp_typed_schema_and_rest_have_same_import_state_contract(self):
        schema = next(t['inputSchema'] for t in mcp.TOOLS if t['name'] == 'bridge_import_record')
        self.assertIn('boolean', schema['properties']['resolved']['type'])
        self.assertIn('array', schema['properties']['paths']['type'])
        server = ready_server(self.store, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with urlopen(f'http://127.0.0.1:{server.server_port}/api/state') as response:
            csrf = json.load(response)['csrf_token']
        for route in ('mcp', 'rest'):
            data = {**self.base, 'ref': route, 'status': 'Open', 'resolved': 'FALSE', 'paths': ['new/a.py']}
            if route == 'mcp':
                mcp.call_tool(self.store, 'bridge_import_record', data)
            else:
                request = Request(f'http://127.0.0.1:{server.server_port}/api/records', data=json.dumps(data).encode(),
                                  headers={'Content-Type': 'application/json', 'X-Bridge-CSRF': csrf}, method='POST')
                with urlopen(request) as response:
                    self.assertEqual(response.status, 200)
            self.assertFalse(_is_settled(self.row(route)))
            self.assertFalse(self.row(route)['resolved'])
