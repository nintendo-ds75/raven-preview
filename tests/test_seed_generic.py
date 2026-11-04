"""The demo seed takes another company's data and runs it through the real
paths: people, authority, the ladder and store.answer for earlier calls (with
the channel the answer came through), make_rule, and the intro notes."""
import sys
from pathlib import Path

from fixtures import OfflineCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import seed_parcelwise_demo as seed  # noqa: E402

PEOPLE = [
    ("Ada Vance", "ada@acme.example", "adavance", "Billing", "member"),
    ("Bo Lin", "bo@acme.example", "bolin", "Data", "member"),
]
TEAMS = [("Billing", "acme/billing", ["Ada Vance"]), ("Data", "acme/data", ["Bo Lin"])]
AUTHORITY = [("Ada Vance", "path", "app/billing/*", "decides", "Invoices"),
             ("Bo Lin", "path", "app/usage/*", "decides", "Usage counts")]
HISTORY = [
    (0, "Ada Vance", ["app/billing/charges.py", "README.md"], "Charges table"),
    (3, "Bo Lin", ["app/usage/counts.py"], "Daily usage counts"),
    (6, "Ada Vance", ["app/billing/charges.py"], "Idempotent charges"),
]
RECORDS = [("ticket", "BILL-1", "2026-02-01T10:00:00+00:00", "Bo Lin", "BILL-1: one use is one completed call",
            "One completed call is one use. Tokens are not units.", ["app/usage/"], {"url": "https://jira/BILL-1"})]
DECISIONS = [
    ("2026-02-02T10:00:00+00:00", "Bo Lin", "What is one use", "What counts as one billable use of the assistant?",
     "app/usage/", "", "One completed call is one use.", "BILL-1 and PR #7.", "github: PR #7 review"),
    ("2026-03-02T10:00:00+00:00", "Ada Vance", "Charges on retry", "Must a charge be safe to post twice?",
     "app/billing/", "", "Yes: an idempotency key per org, feature and period.", "Postmortem.", "meeting: finance sync",
     "same"),
]
NOTES = [("Bo Lin", "Bo owns the usage tables; raw events include internal test orgs.", "role",
          "the raw table has our load tests in it")]


class SeedGenericTest(OfflineCase):
    def test_another_company_through_the_same_paths(self):
        repo = Path(self.temp.name) / "acme-app"
        seed.build_repo(repo, people=PEOPLE, history=HISTORY, codeowners="/app/billing/ @adavance\n")
        db = str(Path(self.temp.name) / "acme.db")
        stats = seed.seed(db, repo, company="Acme", repo_name="acme/acme-app", people=PEOPLE, teams=TEAMS,
                          authority=AUTHORITY, records=RECORDS, decisions=DECISIONS, notes=NOTES)
        self.assertEqual(stats["commits"], 3)
        from raven.store import Store
        store = Store(db)
        graph = store.graph
        self.assertEqual(graph.get_setting("workspace_name"), "Acme")
        self.assertEqual({p["name"] for p in graph.people()}, {"Ada Vance", "Bo Lin"})
        rows = {r["question"]: dict(r) for r in graph.db.execute(
            "SELECT d.*, o.name AS owner_name FROM decisions d LEFT JOIN owners o ON o.id=d.owner_id")}
        use = rows["What counts as one billable use of the assistant?"]
        self.assertEqual((use["owner_name"], use["answered_by"], use["answered_via"]), ("Bo Lin", "Bo Lin", "github"))
        self.assertEqual((use["signoff"], use["reusable"], use["rule_scope"]), ("signed", 1, "any"))
        self.assertEqual(use["created_at"], "2026-02-02T10:00:00+00:00")
        retry = rows["Must a charge be safe to post twice?"]
        self.assertEqual((retry["owner_name"], retry["answered_via"], retry["rule_scope"]), ("Ada Vance", "meeting", "same"))
        record = graph.db.execute("SELECT kind, author FROM intents WHERE ref='BILL-1'").fetchone()
        self.assertEqual(tuple(record), ("ticket", "Bo Lin"))
        self.assertEqual(graph.get_source("acme/acme-app", "url:ticket:BILL-1"), "https://jira/BILL-1")
        note = graph.db.execute("SELECT p.name, c.topic, c.source, c.quote FROM person_context c "
                                "JOIN people p ON p.id=c.person_id").fetchone()
        self.assertEqual(tuple(note), ("Bo Lin", "role", "intro chat", "the raw table has our load tests in it"))

    def test_parcelwise_defaults_still_seed(self):
        repo = Path(self.temp.name) / "parcelwise-app"
        seed.build_repo(repo)
        db = str(Path(self.temp.name) / "pw.db")
        stats = seed.seed(db, repo)
        self.assertEqual(stats["commits"], len(seed.HISTORY))
        from raven.store import Store
        store = Store(db)
        self.assertEqual(len(store.graph.people()), len(seed.PEOPLE))
        answered = store.graph.db.execute("SELECT count(*) FROM decisions WHERE answered_by!='' AND reusable=1").fetchone()[0]
        self.assertEqual(answered, len(seed.DECISIONS))
