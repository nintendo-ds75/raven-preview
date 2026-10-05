"""Routing across paths and inferred identities."""
import datetime
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase
from bridge.store import Store
from bridge.config import Config
from bridge.routing import route, route_ranked
from bridge import canvas
REPO = 'acme/billing'
NOW = datetime.datetime(2026, 6, 30, tzinfo=datetime.timezone.utc)

WES = ("Wes Chen", "wes@acme.example")
PRIYA = ("Priya Natarajan", "priya@acme.example")
MARISOL = ("Marisol Vega", "marisol@acme.example")
DEVON = ("Devon Okafor", "devon@acme.example")


class NamedPathsTests(OfflineCase):
    """Measured live on a fresh install: the agent named usage.json and
    billing/usage.py, the question was judged under usage.json alone
    (where nobody held a clear share) and went to the triage channel,
    while Wes had written every line of billing/usage.py; the task named
    `billing.usage.invoice_cents` and resolved only to the data file; and
    the CODEOWNERS handle @wchen-acme stayed a stranger beside Wes."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "named.db")
        g = self.graph = self.store.graph
        g.set_source(REPO, "git_now", NOW.isoformat())
        for path in ("usage.json", "README.md", "billing/usage.py", "billing/rates.py", "billing/__init__.py",
                     "tests/test_usage.py", "docs/pricing/README.md"):
            g.upsert_artifact(REPO, path)
        for name, email in (WES, PRIYA, MARISOL, DEVON):
            g.upsert_engineer(name, email)
        for i in range(3):
            g.add_change(REPO, f"usage{i}", (NOW - datetime.timedelta(days=4 + i * 7)).isoformat(),
                         ["billing/usage.py"], [(*WES, "author")] + ([(*PRIYA, "reviewed-by")] if i == 0 else []))
        # The data file at the root was touched by everyone once: no clear share there.
        for i, who in enumerate((PRIYA, MARISOL, DEVON, WES)):
            g.add_change(REPO, f"data{i}", (NOW - datetime.timedelta(days=2 + i * 5)).isoformat(),
                         ["usage.json", "README.md"], [(*who, "author")])
        g.add_change(REPO, "tests0", (NOW - datetime.timedelta(days=6)).isoformat(), ["tests/test_usage.py"],
                     [(*PRIYA, "author")])
        g.add_listing(REPO, "codeowners", "/billing/", "@wchen-acme", ord=0)
        with g.transaction():
            for (name, email), uid in ((WES, "UWES"), (PRIYA, "UPRI"), (MARISOL, "UMAR"), (DEVON, "UDEV")):
                g.add_person(name, email=email, slack_id=uid, source="slack")
            g.set_setting("slack_discovery", "1")
            g.set_setting("workspace_name", "Acme")
        self.question = ("Should usage records tagged `internal-load-test` (enterprise-two, 1100 units) be billed "
                         "or excluded from invoice_cents?")

    def test_every_path_the_agent_names_is_a_scope(self):
        ranked = route_ranked(self.graph, REPO, self.question, path="usage.json", also_paths=["billing/usage.py"])
        self.assertTrue(ranked, "nobody routed")
        self.assertEqual(ranked[0][0], WES[0], ranked[:2])
        self.assertTrue(any("billing/usage.py" in ln for ln in ranked[0][1]), ranked[0][1])

    def test_a_node_on_several_paths_goes_to_the_owner_of_the_code(self):
        cfg = Config(model_api="none")
        task = canvas.start_task(self.store, cfg, {
            "title": "Implement usage-based pricing", "repo": REPO, "agent": "test",
            "goal": "Implement billing.usage.invoice_cents(records, rate_cents); usage.json carries an 11x spike "
                    "tagged internal-load-test and nothing decides whether it is billed.",
            "requester": "New Dev <newdev@example.invalid>"})
        self.assertEqual(task["verdict"], "engage", task["why"])
        # The module named in the task is an area, and its author is named at kickoff.
        self.assertIn("billing/usage.py", [a["path"] for a in task["discovery"]["areas"]], task["discovery"]["areas"])
        self.assertEqual(task["discovery"]["people"][0]["name"], WES[0], task["discovery"]["people"])
        node = canvas.add_node(self.store, cfg, {"task_id": task["task_id"], "paths": "usage.json,billing/usage.py",
                                                  "question": self.question,
                                                  "context": "usage.json record tagged internal-load-test."})
        self.assertEqual(node["status"], "pending", node)
        self.assertEqual(node["owner"], WES[0], node.get("owner_evidence"))

    def test_a_dotted_module_name_resolves_to_its_file(self):
        from bridge.resolve import resolve_paths
        hits = resolve_paths(self.graph, REPO, "Implement billing.usage.invoice_cents(records, rate_cents) for every account")
        self.assertEqual(hits[0].path, "billing/usage.py", [(h.path, h.weight) for h in hits])
        self.assertIn("billing.usage.invoice_cents", hits[0].why)
        rates = resolve_paths(self.graph, REPO, "Should billing.rates default to 7 cents?")
        self.assertEqual(rates[0].path, "billing/rates.py", [(h.path, h.weight) for h in rates])
        # A domain or a version is not a module.
        none = resolve_paths(self.graph, REPO, "Mail from ops@acme.example about release 2.3.1")
        self.assertFalse([h for h in none if h.weight >= 0.9], [(h.path, h.why) for h in none])

    def test_an_initial_and_surname_handle_joins_the_person(self):
        from bridge.signals import _resolve_handle
        display = {"wes chen": "Wes Chen", "priya natarajan": "Priya Natarajan", "marisol vega": "Marisol Vega"}
        self.assertEqual(_resolve_handle("@wchen-acme", display), "wes chen")
        self.assertEqual(_resolve_handle("@wchen", display), "wes chen")
        two = {**display, "wanda chen": "Wanda Chen"}
        self.assertNotIn(_resolve_handle("@wchen-acme", two), two)
        picked = route(self.graph, REPO, self.question, path="billing/usage.py")
        self.assertEqual(picked[0], WES[0], picked[1])
        self.assertTrue(any("CODEOWNERS lists Wes Chen" in ln for ln in picked[1]), picked[1])


if __name__ == "__main__":
    unittest.main()
