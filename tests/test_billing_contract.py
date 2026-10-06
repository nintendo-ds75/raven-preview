"""Synthetic-owner demo acceptance: real code/protocol, no real customers."""
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import canvas, proof
from bridge.authz import Actor
from bridge.config import Config
from bridge.mcp import call_tool
from bridge.store import Invalid, Store
from evals.billing_contract import invoice


def event(units, *, account='acme', seat='one', at='2026-10-01T00:00:00Z', origin='customer'):
    return dict(units=units, account=account, seat=seat, at=at, origin=origin)


class BillingBehaviorTests(OfflineCase):
    def test_allowance_boundaries_and_no_cutoff(self):
        for count, cents in [(0, 0), (499, 0), (500, 0), (501, 2), (1000000, 1999000)]:
            with self.subTest(count=count):
                result = invoice([event(count)])[0]
                self.assertEqual(result['amount_cents'], cents)
                self.assertTrue(result['usage_allowed'])

    def test_allowance_is_per_seat_not_pooled(self):
        result = invoice([event(700, seat='one'), event(100, seat='two')])[0]
        self.assertEqual(result['amount_cents'], 400)

    def test_month_reset_utc_boundary_and_no_rollover(self):
        result = invoice([event(499, at='2026-09-30T23:59:59Z'), event(501),
                          event(500, seat='two', at='2026-09-30T20:00:00-04:00')])
        self.assertEqual([r['month'] for r in result], ['2026-09', '2026-10'])
        self.assertEqual([r['amount_cents'] for r in result], [0, 2])

    def test_six_hundred_thousand_internal_calls_do_not_consume_allowance(self):
        result = invoice([event(600000, origin='internal_load_test'), event(501)])[0]
        self.assertEqual(result['excluded_internal_units'], 600000)
        self.assertEqual(result['billable_units'], 501)
        self.assertEqual(result['amount_cents'], 2)

    def test_northwind_hold_is_explicit_scoped_and_does_not_cut_off_usage(self):
        rows = {r['account']: r for r in invoice([event(600, account='northwind'), event(600, account='globex')],
                                                legal_holds={'northwind'})}
        self.assertEqual(rows['northwind']['amount_cents'], 0)
        self.assertEqual(rows['northwind']['deferred_cents'], 200)
        self.assertTrue(rows['northwind']['usage_allowed'])
        self.assertEqual(rows['globex']['amount_cents'], 200)
        self.assertEqual(invoice([event(600, account='northwind')])[0]['amount_cents'], 200)

    def test_invalid_metering_inputs_fail_closed(self):
        for invalid in [event(-1), event(True), event(1.5), event(1, origin='untrusted'),
                        event(1, at='2026-10-01'), event(1, account='')]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                invoice([invalid])


class BillingProtocolTests(OfflineCase):
    def test_complete_synthetic_decision_loop_exports_code_and_reuses_history_without_ping(self):
        store = Store(Path(self.temp.name) / 'billing.db')
        self.addCleanup(store.graph.close)
        cfg = Config(model_api='none')
        graph = store.graph
        pricing = graph.add_person('Pricing owner (synthetic)', email='pricing@example.test')
        finance = graph.add_person('Finance reviewer (synthetic)', email='finance@example.test')
        legal = graph.add_person('Legal owner (synthetic)', email='legal@example.test')
        graph.add_authority('path', 'billing/*', 'decides', person_id=pricing, repo='demo/billing')
        graph.add_authority('path', 'billing/*', 'approves', person_id=finance, repo='demo/billing')
        graph.add_authority('category', 'legal', 'decides', person_id=legal, repo='demo/billing')
        task = canvas.start_task(store, cfg, {'title': 'Add usage-based pricing', 'repo': 'demo/billing',
                    'goal': 'Implement usage pricing and preserve customer-specific legal exceptions.',
                    'agent': 'Deterministic acceptance host', 'paths': 'billing/usage.py',
                    'client_key': 'billing-contract', 'facts': 'release=2026-Q4'})['task_id']
        policies = [
            ('What is the monthly free allowance?', '500 free uses per seat per UTC calendar month; do not pool allowances or roll unused units over.'),
            ('What is the overage price?', 'Charge exactly two integer cents per eligible use above the seat allowance.'),
            ('Should usage be cut off at the allowance?', 'Never cut off usage. The allowance controls billing only.'),
            ('Should 600000 internal load-test calls be billed?', 'Exclude all 600000 trusted internal load-test calls from invoice units and allowance consumption.'),
        ]
        nodes = []
        for number, (question, answer) in enumerate(policies):
            node = canvas.add_node(store, cfg, {'task_id': task, 'question': question, 'paths': 'billing/usage.py',
                        'client_ref': str(number), 'parent_id': nodes[0]['node_id'] if nodes else ''})
            nodes.append(node)
            store.answer(node['node_id'], {'answer': answer, 'rationale': 'Synthetic test policy, explicitly reviewed'},
                         actor=Actor.person(graph.get_person(pricing)))
            self.assertFalse(canvas.node_view(store, node['node_id'])['authorized'])
            canvas.sign_off(store, node['node_id'], {'expected_updated_at': store.get_decision(node['node_id'])['updated_at']},
                            actor=Actor.person(graph.get_person(finance)))
        exception = canvas.add_node(store, cfg, {'task_id': task, 'question': 'May Northwind overage invoices issue before MFN review?',
                    'category': 'legal', 'paths': 'billing/usage.py', 'facts': 'customer=northwind', 'client_ref': 'legal',
                    'parent_id': nodes[0]['node_id']})
        with self.assertRaises(Invalid):
            canvas.finish_task(store, {'task_id': task})
        # Explicit synthetic decision resolves semantics the demo leaves unspecified.
        # It is not represented as a real customer's contract or real legal advice.
        store.answer(exception['node_id'], {'answer': 'Defer Northwind overage invoices until Legal clears MFN review; '
                     'track accrued overage separately and keep service available. No other customer inherits this hold.',
                     'rationale': 'Synthetic acceptance scenario'}, actor=Actor.person(graph.get_person(legal)))
        for pid in (pricing, finance):
            current = canvas.node_view(store, exception['node_id'])
            if graph.get_person(pid)['name'] in current['required_signers'] and graph.get_person(pid)['name'] not in current['signatures']:
                canvas.sign_off(store, exception['node_id'], {'expected_updated_at': current['updated_at']},
                                actor=Actor.person(graph.get_person(pid)))
        source = Path(__file__).resolve().parents[1] / 'evals/billing_contract.py'
        code = source.read_text()
        diff = ('diff --git a/billing/usage.py b/billing/usage.py\nnew file mode 100644\n--- /dev/null\n+++ b/billing/usage.py\n'
                + f'@@ -0,0 +1,{len(code.splitlines())} @@\n' + ''.join('+' + line + '\n' for line in code.splitlines()))
        canvas.get_tree(store, task)
        with patch('bridge.llm.check_conformance', return_value=None):
            done = canvas.finish_task(store, {'task_id': task, 'diff': diff,
                    'checks': 'Independent test_billing_contract.BillingBehaviorTests execute the reference implementation.'})
        self.assertEqual(done['status'], 'completed')
        exported = proof.export(store, {'task_id': task})
        self.assertEqual(exported['bundle']['payload']['change']['sha256'], hashlib.sha256(diff.encode()).hexdigest())
        self.assertEqual(len(exported['bundle']['payload']['decisions']), 5)
        before = graph.db.execute('SELECT count(*) AS n FROM notifications').fetchone()['n']
        second = Store(store.path)
        try:
            found = call_tool(second, 'bridge_search_decisions', {'query': 'internal load-test calls', 'repo': 'demo/billing'})
            self.assertTrue(found['matches'])
            original = call_tool(second, 'bridge_get_decision', {'decision_id': nodes[-1]['node_id']})
            self.assertIn('600000', original['answer'])
            self.assertIn('synthetic', original['signed_by'])
            self.assertEqual(before, graph.db.execute('SELECT count(*) AS n FROM notifications').fetchone()['n'])
        finally:
            second.graph.close()
