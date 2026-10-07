"""Actual bound scope rendered by shipped JS, with no provider or identity probes."""
import http.client
import json
import math
import threading
from pathlib import Path
import shutil
import subprocess
import unittest

from fixtures import OfflineCase, ROOT, ready_server
from bridge import approval_scope, briefing
from bridge.store import Store
from owner_context_fixture import seed
from test_agent_source_web import HTMLNodes


class OwnerContextFixture:
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'owner-context.db')
        self.addCleanup(self.store.graph.close)
        self.fixture = seed(self.store)

    def row(self):
        return self.store.get_decision(self.fixture['node_id'])

    def raw(self):
        return dict(self.store.graph.db.execute('SELECT * FROM decisions WHERE id=?',
                    (self.fixture['node_id'],)).fetchone())

    def brief(self):
        link = briefing.resolve(self.store.graph, self.fixture['token'])
        return briefing.overview(self.store, link)


class OwnerContextStorageTests(OwnerContextFixture, OfflineCase):
    def set_facts(self, literal):
        self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
            ('{"nested": {"values": [' + literal + ']}}', self.fixture['node_id']))

    def test_unsafe_numeric_display_export_keeps_exact_scope_and_bindings(self):
        for literal in ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400', '-0.0', '-0e0', '9007199254740993'):
            with self.subTest(literal=literal):
                self.set_facts(literal)
                raw = self.raw()
                baseline_revision = approval_scope.revision(raw)
                baseline_hash = approval_scope.scope_hash(raw)
                for row in (self.row(), self.brief()['focus']):
                    self.assertIsNone(row['approval_scope'])
                    self.assertEqual(row['approval_scope_text'], approval_scope.render(raw))
                    json.dumps(row, allow_nan=False)
                after = self.raw()
                self.assertEqual(after, raw)
                self.assertEqual(approval_scope.revision(after), baseline_revision)
                self.assertEqual(approval_scope.scope_hash(after), baseline_hash)
                self.assertIn(literal, after['facts'])

    def test_actual_decision_and_brief_wire_json_is_strict_for_unsafe_numbers(self):
        server = ready_server(self.store, port=0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def reject_constant(value):
            raise ValueError('Not strict JSON: ' + value)

        def finite_float(value):
            result = float(value)
            if not math.isfinite(result):
                raise ValueError('Overflow in wire JSON: ' + value)
            return result

        for literal in ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400', '-0.0', '-0e0'):
            with self.subTest(literal=literal):
                self.set_facts(literal)
                raw = self.raw()
                for route in ('/api/decisions/' + self.fixture['node_id'], '/api/brief'):
                    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
                    connection.request('GET', route, headers={'X-Raven-Link': self.fixture['token']})
                    response = connection.getresponse()
                    wire = response.read().decode()
                    connection.close()
                    self.assertEqual(response.status, 200, wire)
                    parsed = json.loads(wire, parse_constant=reject_constant, parse_float=finite_float)
                    row = parsed['focus'] if route == '/api/brief' else parsed
                    self.assertIsNone(row['approval_scope'])
                    self.assertEqual(row['approval_scope_text'], approval_scope.render(raw))
                self.assertEqual(self.raw(), raw)

    def test_safe_numbers_and_numeric_looking_strings_keep_structured_display(self):
        facts = {'zero': 0, 'fraction': 0.5, 'negative': -2, 'large_safe': 9007199254740991,
                 'text': ['NaN', 'Infinity', '-Infinity', '1e400', '-0.0']}
        self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
            (json.dumps(facts), self.fixture['node_id']))
        for row in (self.row(), self.brief()['focus']):
            self.assertEqual(row['approval_scope']['facts'], facts)
            json.dumps(row, allow_nan=False)

    def test_deep_legacy_scope_retains_exact_text_without_recursive_export(self):
        literal = '{"deep":' + '[' * 500 + '0' + ']' * 500 + '}'
        self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
            (literal, self.fixture['node_id']))
        raw = self.raw()
        expected = approval_scope.render(raw)
        baseline_hash = approval_scope.scope_hash(raw)
        baseline_revision = approval_scope.revision(raw)
        for row in (self.row(), self.brief()['focus']):
            self.assertIsNone(row['approval_scope'])
            self.assertEqual(row['approval_scope_text'], expected)
            json.dumps(row, allow_nan=False)
        self.assertEqual(self.raw(), raw)
        self.assertEqual(approval_scope.scope_hash(self.raw()), baseline_hash)
        self.assertEqual(approval_scope.revision(self.raw()), baseline_revision)

    def test_both_reads_expose_exact_signature_scope_without_mutation(self):
        raw = self.raw()
        baseline = self.row()
        scope = approval_scope.snapshot(raw)
        revision = approval_scope.revision(baseline)
        hashed = approval_scope.scope_hash(baseline)
        brief = self.brief()
        for row in (baseline, brief['focus']):
            self.assertEqual(row['approval_scope'], scope)
            self.assertEqual(set(row['approval_scope_labels']), set(scope))
            self.assertEqual(row['approval_scope_text'], approval_scope.render(raw))
        self.assertEqual(brief['task']['goal'], self.fixture['request'])
        self.assertEqual(brief['task']['title'], self.fixture['request'][:300])
        self.assertEqual(self.raw(), raw)
        after = self.row()
        self.assertEqual(approval_scope.revision(after), revision)
        self.assertEqual(approval_scope.scope_hash(after), hashed)
        for key in ('sources', 'source_revalidation', 'authorized', 'signatures', 'signed_hash', 'signed_revision'):
            self.assertEqual(after[key], baseline[key], key)

    def test_native_scope_revision_keeps_every_field_bound_after_display(self):
        row = self.row()
        revision = approval_scope.revision(row)
        raw = self.raw()
        self.assertEqual(row['approval_scope'], approval_scope.snapshot(raw))
        self.assertEqual(self.raw(), raw)
        for key, value in row['approval_scope'].items():
            with self.subTest(field=key):
                changed = {**row, key: 'different stored value'}
                self.assertNotEqual(approval_scope.revision(changed), revision)
        # Presentation-only additions do not enter the signature inventory.
        self.assertEqual(approval_scope.revision({**row, 'approval_scope_labels': {'facts': 'Other label'}}), revision)


@unittest.skipUnless(shutil.which('node'), 'Node is required for the shipped renderers')
class OwnerContextDOMTests(OwnerContextFixture, OfflineCase):
    def render(self, row, task=None):
        code = r"""
const fs=require('node:fs'),vm=require('node:vm');
const app=fs.readFileSync('web/app.js','utf8'), c={}; vm.createContext(c);
vm.runInContext(app.slice(app.indexOf('const esc ='),app.indexOf('const plural =')),c);
vm.runInContext(fs.readFileSync('web/presentation.js','utf8'),c);
const d=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify({scope:c.approvalScope(d.row), label:c.taskDisplayLabel(d.task,d.row,'Decision review')}));
"""
        out = subprocess.run(['node', '-e', code], cwd=ROOT, input=json.dumps({'row': row, 'task': task or {}}),
                             text=True, capture_output=True, check=True, timeout=10)
        return json.loads(out.stdout)

    def test_nested_falsy_and_markup_values_are_complete_and_escaped(self):
        facts = {'customer': 'Synthetic  North', 'zero': 0, 'false': False, 'null': None,
                 'empty': '', 'empty_list': [], 'empty_map': {}, '<img src=x onerror=bad()>': '<script>bad()</script>',
                 'nested': {'a': [0, False, None, '', {'unicode': '日本語 & <b>literal</b>'}]},
                 'long': 'unbroken' * 200, 'whitespace': ' leading\ntrailing  '}
        self.store.graph.db.execute('UPDATE decisions SET facts=?,applicability=?,options=? WHERE id=?',
            (json.dumps(facts), json.dumps({'customer': ['Synthetic  North'], 'exclude': {'x': False}}),
             'malformed legacy <svg/onload=bad()>', self.fixture['node_id']))
        row = self.row()
        html = HTMLNodes(self.render(row)['scope'])
        self.assertFalse(any(n['tag'] in ('script', 'img', 'svg') for n in html.nodes))
        exact = html.with_class('scope-json')[0]
        self.assertEqual(json.loads(exact['text']), approval_scope.snapshot(self.raw()))
        self.assertEqual(html.with_class('scope-text')[0]['text'], row['approval_scope_text'])
        fields = {n['attrs']['data-scope-field']: n['text'] for n in html.nodes if 'data-scope-field' in n['attrs']}
        for text in ('Synthetic  North', '0', 'false', 'null', '<script>bad()</script>',
                     '日本語 & <b>literal</b>', 'unbroken' * 200, ' leading\ntrailing  '):
            self.assertIn(text, fields['facts'])
        for key in facts:
            self.assertIn(key, fields['facts'])
        self.assertIn('Empty text', fields['facts'])
        self.assertIn('Empty list', fields['facts'])
        self.assertIn('No recorded values', fields['facts'])
        self.assertEqual(fields['options'], row['approval_scope']['options'])
        self.assertIn('false', fields['applicability'])

    def test_display_labels_use_only_short_exact_titles_or_structured_identifiers(self):
        row = self.row()
        self.assertEqual(self.render(row, {'title': self.fixture['request'][:300]})['label'],
                         'Decision review · WORK-42')
        self.assertEqual(self.render(row, {'title': 'A clear <human> title'})['label'], 'A clear <human> title')
        for title in ('x' * 300, 'first line\nsecond line', 'word ' * 80):
            self.assertEqual(self.render({}, {'title': title})['label'], 'Decision review')
        self.assertEqual(self.render({}, {'title': 'x' * 300, 'facts': {'work_item': 'EXACT-2'}})['label'],
                         'Decision review · EXACT-2')
        self.assertEqual(self.render({}, {'title': 'Mention PROSE-99 ' * 25, 'facts': {'work_item': ['WRONG-TYPE']}})['label'],
                         'Decision review')

    def test_legacy_payload_retains_original_scope_without_guessing_fields(self):
        text = 'Legacy context: <unparsed> customer=Do not infer & complete text'
        html = HTMLNodes(self.render({'approval_scope_text': text})['scope'])
        self.assertEqual(html.with_class('approval-scope')[0]['text'], text)
        self.assertFalse(html.with_class('scope-fields'))

    def test_large_integer_uses_exact_server_text_instead_of_rounded_json(self):
        self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
            (json.dumps({'large': 9007199254740993}), self.fixture['node_id']))
        row = self.row()
        html = HTMLNodes(self.render(row)['scope'])
        self.assertEqual(html.with_class('approval-scope')[0]['text'], row['approval_scope_text'])
        self.assertIn('9007199254740993', html.with_class('approval-scope')[0]['text'])
        self.assertNotIn('9007199254740992', html.with_class('approval-scope')[0]['text'])

    def test_unsafe_numbers_parse_and_render_exact_scope_fallback(self):
        for literal in ('NaN', 'Infinity', '-Infinity', '1e400', '-1e400', '-0.0', '-0e0'):
            with self.subTest(literal=literal):
                self.store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?',
                    ('{"value": ' + literal + '}', self.fixture['node_id']))
                for row in (self.row(), self.brief()['focus']):
                    html = HTMLNodes(self.render(row)['scope'])
                    self.assertEqual(html.with_class('approval-scope')[0]['text'], row['approval_scope_text'])
                    self.assertFalse(html.with_class('scope-json'))
                    if literal.startswith('-0'):
                        self.assertIn('-0.0', html.with_class('approval-scope')[0]['text'])

    def test_full_original_request_is_rendered_separately_and_escaped(self):
        brief = self.brief()
        code = r"""
const fs=require('node:fs'),vm=require('node:vm'),c={avatar:()=>''};vm.createContext(c);
const app=fs.readFileSync('web/app.js','utf8'),brief=fs.readFileSync('web/brief.js','utf8');
vm.runInContext(app.slice(app.indexOf('const esc ='),app.indexOf('const plural =')),c);
vm.runInContext(brief.slice(brief.indexOf('function requesterCard('),brief.indexOf('function decisionsCard(')),c);
const data=JSON.parse(fs.readFileSync(0,'utf8'));console.log(c.requesterCard(data.task,data.requester));
"""
        out = subprocess.run(['node', '-e', code], cwd=ROOT, input=json.dumps(brief), text=True,
                             capture_output=True, check=True, timeout=10)
        html = HTMLNodes(out.stdout)
        self.assertEqual(html.with_class('brief-prompt')[0]['text'], self.fixture['request'])
        self.assertFalse(any(n['tag'] == 'retain' for n in html.nodes))


if __name__ == '__main__':
    unittest.main()
