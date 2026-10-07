"""Actual source API data rendered by shipped JS and inspected as HTML nodes.

These checks require Node, but no browser, providers, or network connection.
The separate browser suite exercises clicks and phone layout when Chromium runs.
"""
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import unittest

from fixtures import OfflineCase, ROOT
from bridge import canvas, context_memory as cm
from bridge.store import Invalid, Store


class HTMLNodes(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': dict(attrs), 'text': ''}
        self.nodes.append(node)
        if tag not in ('input', 'br', 'img', 'hr', 'meta', 'link'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]['tag'] == tag:
                del self.stack[i:]
                return

    def handle_data(self, text):
        for node in self.stack:
            node['text'] += text

    def with_class(self, name):
        return [n for n in self.nodes if name in n['attrs'].get('class', '').split()]


@unittest.skipUnless(shutil.which('node'), 'Node is required to run the shipped source renderers')
class AgentSourceWebTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / 'source-web.db')
        self.g = self.store.graph
        self.addCleanup(self.g.close)
        self.store.add_owner({'name': 'Source Reviewer', 'team': 'Policy', 'patterns': 'policy/*'})
        self.repo = 'synthetic/source-web'
        self.task = self.store.add_run({'title': 'Review source snapshots', 'repo': self.repo})['id']
        self.did = self.g.add_decision(self.task, 'Apply this archive policy?', 'policy', 'pending',
            repo=self.repo, owner='Source Reviewer', path='policy/archive.py')
        self.data = [{'repo': self.repo, 'kind': 'jira', 'provider': 'jira', 'namespace': 'fixture.example',
            'ref': f'WEB-{i}', 'title': f'Source {i}', 'body': f'COMPLETE BODY {i}\n<img src=x onerror="throw 1"> & END {i}',
            'status': 'Done', 'paths': ['policy/archive.py'], 'url': f'https://example.invalid/WEB-{i}'} for i in (1, 2)]
        self.records = [self.store.add_record(data)['source'] for data in self.data]

    def propose(self, role='support'):
        canvas.settle_node(self.store, {'task_id': self.task, 'node_id': self.did,
            'answer': 'Apply this policy to the scoped archive.',
            'source_evidence': [cm.pin(r, role) for r in self.records]})

    def render(self, row):
        code = r"""
const fs=require('node:fs'),vm=require('node:vm');
const app=fs.readFileSync('web/app.js','utf8'),brief=fs.readFileSync('web/brief.js','utf8');
const d=JSON.parse(fs.readFileSync(0,'utf8')),context={};vm.createContext(context);
vm.runInContext(app.slice(app.indexOf('const esc ='),app.indexOf('const plural =')),context);
vm.runInContext(app.slice(app.indexOf('function sourceSnapshots('),app.indexOf('function sourceReplacementField(')),context);
vm.runInContext(brief.slice(brief.indexOf('function sourceReview('),brief.indexOf('function focusCard(')),context);
console.log(JSON.stringify({source:context.sourceEvidence(d.sources,d.needs_review?null:d.source_revalidation),
 revalidation:context.sourceRevalidationFields(d,'signoff'),brief:context.sourceReview(d)}));
"""
        result = subprocess.run(['node', '-e', code], cwd=ROOT, input=json.dumps(row), text=True,
                                capture_output=True, check=True, timeout=10)
        return json.loads(result.stdout)

    def assert_snapshots(self, html, row, brief=False):
        parsed = HTMLNodes(html)
        blocks = parsed.with_class('source-snapshot')
        self.assertEqual(len(blocks), len(self.records))
        self.assertFalse(any(n['tag'] in ('img', 'script', 'svg') for n in parsed.nodes))
        source_map = {s['record_id']: s for s in row['source_revalidation']['sources']}
        for block in blocks:
            record = next(r for r in self.records if r['record_id'] in block['text'])
            source = source_map[record['record_id']]
            for value in (record['record_id'], source['source_version_id'], source['role'], source['snapshot']['body']):
                self.assertIn(value, block['text'])
            metadata = json.dumps({k: v for k, v in source['snapshot'].items() if k != 'body'}, indent=2, ensure_ascii=False)
            self.assertIn(metadata, block['text'])
        return parsed

    def test_fresh_api_sources_display_complete_snapshots_without_revalidation_controls(self):
        self.propose()
        row = self.store.get_decision(self.did)
        self.assertEqual(row['needs_review'], 0)
        rendered = self.render(row)
        parsed = self.assert_snapshots(rendered['source'], row)
        self.assertFalse(any(n['tag'] == 'input' for n in parsed.nodes))
        self.assertEqual(rendered['revalidation'], '')
        brief = self.assert_snapshots(rendered['brief'], row, brief=True)
        self.assertTrue(any(n['attrs'].get('id') == 'brief-source-confirm' for n in brief.nodes))
        signed = canvas.sign_off(self.store, self.did, {'by': 'Source Reviewer', 'expected_updated_at': row['updated_at']})
        self.assertTrue(signed['authorized'])
        self.assertCountEqual([cm.pin(s, s['role']) for s in self.store.get_decision(self.did)['sources']],
                              [cm.pin(r) for r in self.records])

    def test_context_only_old_snapshots_are_visible_and_remain_nonblocking(self):
        self.propose('context')
        self.store.add_record({**self.data[1], 'body': 'Later informational version.'})
        row = self.store.get_decision(self.did)
        self.assertEqual(row['needs_review'], 0)
        self.assertFalse(row['source_revalidation']['has_reliance'])
        rendered = self.render(row)
        self.assert_snapshots(rendered['source'], row)
        brief = self.assert_snapshots(rendered['brief'], row, brief=True)
        self.assertFalse(any(n['tag'] == 'input' for n in brief.nodes))
        self.assertNotIn('Signing waits', rendered['brief'])
        self.assertEqual(rendered['revalidation'], '')
        signed = canvas.sign_off(self.store, self.did, {'by': 'Source Reviewer', 'expected_updated_at': row['updated_at']})
        self.assertTrue(signed['authorized'])
        self.assertEqual(self.store.get_decision(self.did)['needs_review'], 0)

    def test_changed_second_source_still_refuses_the_fresh_displayed_signoff(self):
        self.propose()
        shown = self.store.get_decision(self.did)
        self.assert_snapshots(self.render(shown)['source'], shown)
        changed = self.store.add_record({**self.data[1], 'body': 'Changed second premise.'})
        with self.assertRaises(Invalid):
            canvas.sign_off(self.store, self.did, {'by': 'Source Reviewer', 'expected_updated_at': shown['updated_at']})
        current = self.store.get_decision(self.did)
        self.assertFalse(current['authorized'])
        self.assertTrue(current['needs_review'])
        parsed = HTMLNodes(self.render(current)['revalidation'])
        self.assertTrue(any(n['attrs'].get('id') == 'source-review-signoff' for n in parsed.nodes))
        pin_input = next(n for n in parsed.nodes if n['attrs'].get('id') == 'source-pins-signoff')
        self.assertIn(changed['source']['source_version_id'], pin_input['attrs']['value'])
