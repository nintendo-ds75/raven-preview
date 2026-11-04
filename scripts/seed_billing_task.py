"""Add the larger live task to the Parcelwise demo: Claude Code turning on
overage billing for the Starter plan, for Maya Chen. Its seven judgement
calls cover every state the Being built page shows: settled from strong
precedent, a close precedent a person confirmed, novel calls people
answered, and novel calls still waiting on someone.

    python scripts/seed_billing_task.py fixtures/parcelwise-demo.db

Running it again does nothing: the task is keyed by its client_key.
"""
import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from raven.store import Store  # noqa: E402

REPO = 'parcelwise/parcelwise-app'
KEY = 'starter-overage-billing-1'
DAY = '2026-11-01T'

TASK = {
    'title': 'Turn on overage billing for the Starter plan',
    'goal': ("Turn on billing for labels past the Starter plan's 1,000 a month. Charge through Stripe from the "
             "December invoices, show merchants their usage, and don't surprise anyone on a negotiated rate."),
}

# (ref, question, kind, owner, status, answered_by, answer, rationale, source_id, context, path, created, updated)
NODES = [
    ('overage-price-first', 'Must a Starter merchant see the overage price before Raven buys a label past their allowance?',
     'evidence', 'Ana Ruiz', 'resolved', 'Ana Ruiz', 'Never buy a label before the merchant sees the price.', '',
     '58a3ba4f2afe',
     'Prior decision 58a3ba4f2afe (Ana Ruiz): never buy a label before the merchant sees the price. Postmortem 2026-06-02 '
     '(labels bought at a stale rate) and ADR 0003 (docs/adr/0003-merchant-sees-the-price-first.md) say the same. '
     'An overage label is a label with a different price, so the rule covers it.',
     'app/labels/purchase.py', '09:02:10', '09:02:14'),
    ('billable-label-path', 'Where is a billable label recorded: inside purchase_label() or in each caller?',
     'evidence', 'Jordan Okafor', 'resolved', 'Jordan Okafor',
     'Every label purchase goes through purchase_label() with an idempotency key.', '', '92310f00ef1d',
     'Prior decision 92310f00ef1d (Jordan Okafor): every label purchase goes through purchase_label() with an idempotency '
     'key. ADR 0004 (docs/adr/0004-one-purchase-path.md:19); tests/test_single_purchase_path.py fails the build if '
     'anything else buys. Recording usage there, keyed by the idempotency key, means a retry never bills twice.',
     'app/labels/purchase.py', '09:02:40', '09:02:44'),
    ('overage-price', 'What does a Starter merchant pay per label past 1,000 a month?',
     'prediction', 'Priya Shah', 'approved', 'Priya Shah',
     '$0.08 per label past the allowance, billed monthly in arrears. Growth stays unlimited.',
     'Matches what we quoted in the pricing thread, and arrears keeps the purchase flow free of card checks.', '',
     'app/billing/plans.py:3-17: free=50, starter=1,000/month, growth=unlimited. Slack #pricing (Oct 14): Priya floated '
     '$0.08 per overage label. Raven predicted $0.08 in arrears and asked Priya to confirm.',
     'app/billing/plans.py', '09:03:30', '09:41:05'),
    ('usage-metering', 'Which system meters usage and sends the invoice: Stripe metered billing, or our own invoice job?',
     'new', 'Marcus Lee', 'approved', 'Marcus Lee',
     'Stripe Billing meters it. The label-worker reports each billable label to Stripe, idempotent by label id. No invoice job of our own.',
     'One more job of ours is one more thing to page on. Stripe already holds the customer and the card.', '',
     'No decision on record. ADR 0005 (docs/adr/0005-label-worker.md) added the job table and label-worker. '
     'PR #118 synced merchants to Stripe customers but nothing meters usage. Jira PAY-42 asks for metered billing. '
     'infra/deploy.yaml has no billing service.',
     'app/billing/stripe.py', '09:04:12', '10:18:40'),
    ('usage-in-dashboard', 'Where do merchants see their label usage and the overage they will be billed this month?',
     'new', 'Lena Park', 'pending', '', '', '', '',
     'No decision on record. Lena told Raven no new top-level pages without a strong reason (voice intro). Candidates: '
     'a usage bar on web/billing/BillingPage.tsx, or a banner on web/orders/OrdersTable.tsx when a merchant passes 80%.',
     'web/billing/BillingPage.tsx', '09:05:01', '09:05:01'),
    ('negotiated-rates', 'Bloom & Co and two other merchants are on negotiated rates. Do they move to overage billing now, or at contract renewal?',
     'new', 'Sam Rivera', 'pending', '', '', '', '',
     'No decision on record. Jira CS-311 lists the three contracts; Slack #cs-escalations (Oct 22): Bloom asked whether '
     'pricing changes mid-contract. app/billing/contracts.py stores a rate override but no end date.',
     'app/billing/contracts.py', '09:05:40', '09:05:40'),
    ('voided-label-credit', 'When a carrier refunds a voided label, do we take that label back off the overage on the next invoice?',
     'new', 'Ana Ruiz', 'pending', '', '', '', '',
     'No decision on record. app/labels/void.py marks the label void when USPS confirms the refund, which can take up to '
     '30 days, so the refund can land after the invoice that charged for it.',
     'app/labels/void.py', '09:06:15', '09:06:15'),
]


def main(db_path):
    store = Store(db_path)
    db = store.graph.db
    if db.execute('SELECT 1 FROM runs WHERE client_key=?', (KEY,)).fetchone():
        print('Billing task already there.')
        return
    owners = {r['name']: r['id'] for r in db.execute('SELECT id, name FROM owners')}
    people = {p['name']: p['id'] for p in store.graph.people(active_only=False)}
    run_id = uuid.uuid4().hex[:12]
    last = max(DAY + n[12] for n in NODES) + '+00:00'
    with store.graph.transaction():
        db.execute("INSERT INTO runs(id, title, agent, repo, status, updated_at, goal, requester, paths, verdict, "
                   "verdict_why, discovery, client_key, facts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (run_id, TASK['title'], 'Claude Code', REPO, 'needs_judgment', last, TASK['goal'], 'Maya Chen',
                    'app/billing/', 'engage', 'billing touches pricing, payments and merchant contracts: calls for people',
                    json.dumps({'repo': REPO, 'requester': 'Maya Chen'}), KEY, ''))
        for (ref, question, kind, owner, status, by, answer, why, source_id, context, path, created, updated) in NODES:
            created, updated = DAY + created + '+00:00', DAY + updated + '+00:00'
            source_rev = db.execute('SELECT updated_at FROM decisions WHERE id=?', (source_id,)).fetchone()[0] if source_id else ''
            settled = kind == 'evidence'
            db.execute(
                "INSERT INTO decisions(id, run_id, question, context, path, owner_id, routing_reason, status, source_id, "
                "source_revision, answer, rationale, answered_by, created_at, updated_at, category, source, evidence, "
                "repo, kind, origin, signoff, signed_by, actor_id, actor_name, actor_basis, client_ref, scope_paths, draft) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (uuid.uuid4().hex[:12], run_id, question, context, path, owners[owner], 'resolution ladder', status,
                 source_id or None, source_rev, answer or None, why or None, by or None, created, updated,
                 'policy', 'memory' if settled else ('human' if by else ''),
                 f'covered by the rule {by} made of decision {source_id}' if settled else '',
                 REPO, kind, 'agent', 'rule' if settled else ('signed' if by else ''), by,
                 '' if settled or not by else people.get(by, ''), '' if settled else by,
                 '' if settled or not by else 'owner', ref, json.dumps([path])))
    store.graph._bump('') if hasattr(store.graph, '_bump') else None
    print(f'Added "{TASK["title"]}" ({run_id}) with {len(NODES)} decisions.')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'fixtures/parcelwise-demo.db')
