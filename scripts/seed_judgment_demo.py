"""Give the Parcelwise demo the judgment-log signals the Being built page
shows: how each person's sign-off arrived, why each precedent Raven applied
fits, and one real correction, so the page has a replaced call to show.

    python scripts/seed_judgment_demo.py fixtures/parcelwise-demo.db

The channel and fit lines are fixture data for rows the demo seeded by hand
(a live Raven records them itself: bridge/judgment.py). The correction is
not: it runs through judgment.correct as Jordan, the same path the page's
"That's wrong" action takes, so the old call is retired and the correction
is the standing rule exactly as it would be in a real workspace.

Running it again does nothing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import judgment  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.store import Store  # noqa: E402

# question prefix -> (how the answer arrived, why the precedent fits)
ROWS = {
    'Can a label be bought before the merchant has seen': ('meeting', ''),
    'May any code path other than purchase_label() buy a label': ('github', ''),
    'When we buy or reprint labels for many orders at once': ('slack', ''),
    'For a bulk buy of ~200 USPS labels': ('slack', ''),
    'In a bulk buy, the merchant approves one total up front': ('slack', ''),
    'The Orders page shows 50 orders per page': ('voice call', ''),
    "If a bulk buy would take a merchant past their plan's monthly": ('slack', ''),
    'Bulk label buys must run as a background job': ('browser', ''),
    'What does a Starter merchant pay per label past 1,000': ('slack', ''),
    'Which system meters usage and sends the invoice': ('voice call', ''),
    'When we buy labels for many orders at once, do we call the carrier': (
        '', "The same question Jordan Okafor answered on Oct 28 for the batch-of-orders task; "
            "Jordan said that answer applies anywhere, so nobody needed to be asked."),
    'Must a Starter merchant see the overage price': (
        '', "An overage label is a label at a different price, which is what Ana Ruiz's Jun 2 call is about; "
            "Ana said that answer applies anywhere, so nobody needed to be asked."),
    'Where is a billable label recorded': (
        '', "Recording usage inside purchase_label() is the one-purchase-path question Jordan Okafor settled on Sep 9; "
            "Jordan said that answer applies anywhere, so nobody needed to be asked."),
}

CORRECTION = {
    'question': 'When we buy labels for many orders at once, do we call the carrier',
    'answer': ("Once per order through purchase_label() on our shared USPS account. On a merchant's own carrier "
               "account, like Bloom's, send the batch as one USPS manifest call, and still record each label with "
               "its own idempotency key."),
    'why': ("The Oct 28 call was about our shared USPS account. Bloom buys on its own account, where USPS wants one "
            "manifest per batch and throttles per-order calls."),
}


def main(db_path):
    store = Store(db_path)
    db = store.graph.db
    with store.graph.transaction():
        for prefix, (via, fit) in ROWS.items():
            for row in db.execute('SELECT id, answered_via, fit_reason FROM decisions WHERE question LIKE ?', (prefix + '%',)):
                if via and not row['answered_via']:
                    db.execute('UPDATE decisions SET answered_via=? WHERE id=?', (via, row['id']))
                if fit and not row['fit_reason']:
                    db.execute('UPDATE decisions SET fit_reason=? WHERE id=?', (fit, row['id']))
    target = db.execute("SELECT d.id, d.updated_at FROM decisions d JOIN runs r ON r.id=d.run_id WHERE d.question LIKE ? "
                        "AND r.status NOT IN ('completed','failed','cancelled','abandoned') AND d.source_id IS NOT NULL",
                        (CORRECTION['question'] + '%',)).fetchone()
    if target is None:
        print('Correction already recorded (or nothing settled to correct).')
        return
    jordan = store.graph.find_person('jordan@parcelwise.example')
    actor = Actor(id=jordan['id'], name=jordan['name'], role=jordan.get('role') or 'member', kind='session')
    result = judgment.correct(store, target['id'], {'answer': CORRECTION['answer'], 'why': CORRECTION['why'],
                                                    'scope': 'everywhere', 'expected_updated_at': target['updated_at'],
                                                    'source': f"inbox: {jordan['name']}"}, actor=actor)
    print(f"Jordan corrected {target['id']}: {result['notice']}")


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'fixtures/parcelwise-demo.db')
