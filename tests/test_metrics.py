"""What Raven saved, measured from the ledger: proposal outcomes, time to
answer and to signature, re-asks, reuse, hand-ons and the agent wait
avoided, per workspace and per repository, over a window; readable by a
member over HTTP and in bridge_connection_status."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from bridge import canvas, metrics  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.store import Invalid  # noqa: E402

REPO = "acme/platform"
QUESTION = "Should we bill the usage spike?"
HOUR = 3600


class SeededStore(OfflineCase):
    """A workspace with the histories the numbers are read from, written
    through the canvas protocol and the inbox's own writes."""

    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("metrics.db")
        self.cfg = Config(model_api="none")
        self.store.add_owner({"name": "Wes Chen", "team": "Data", "patterns": "billing/*"})
        self.store.add_owner({"name": "Mara Ortiz", "team": "Pricing", "patterns": "pricing/*"})

    def task(self, title, key, repo=REPO):
        return canvas.start_task(self.store, self.cfg, {"title": title, "agent": "test", "repo": repo, "client_key": key,
                                                        "paths": "billing/usage.py", "requester": "Priya"})["task_id"]

    def node(self, task, question=QUESTION, path="billing/usage.py", category=""):
        return canvas.add_node(self.store, self.cfg, {"task_id": task, "question": question, "paths": path, "category": category})

    def proposal(self, ident, state, *, created_hours, updated_hours=None, rationale="", payload=None, repo=REPO):
        """A row in the proposal ledger as the objective head and the
        person's action on it would have left it."""
        stamp = lambda hours: (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()  # noqa: E731
        with self.store.graph.transaction() as db:
            db.execute("INSERT INTO objective_proposals(id,objective_id,repository,fingerprint,payload,sources,state,rationale,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (ident, "", repo, "fp-" + ident, json.dumps(payload or {"title": "Move " + ident}), "[]", state, rationale,
                        stamp(created_hours), stamp(updated_hours if updated_hours is not None else created_hours)))

    def settle(self, task, node_id, answer):
        canvas.settle_node(self.store, {"task_id": task, "node_id": node_id, "answer": answer, "rationale": "why"})

    def sign(self, node_id, **extra):
        current = canvas.node_view(self.store, node_id)
        return canvas.sign_off(self.store, node_id, {"by": "Wes Chen", "expected_updated_at": current["updated_at"], **extra})

    def answer(self, node_id, text):
        current = self.store.get_decision(node_id)
        return self.store.answer(node_id, {"answer": text, "rationale": "why", "expected_updated_at": current["updated_at"]})

    def backdate(self, decision_id, hours):
        """Move a decision's creation into the past, so the time a person
        took on it is known."""
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        with self.store.graph.transaction() as db:
            db.execute("UPDATE decisions SET created_at=? WHERE id=?", (stamp, decision_id))

    def seed(self):
        """Seven decisions in one repository: an answered question (4 h),
        an agent proposal signed unchanged (1 h), one corrected (3 h), a
        question handed on and answered (10 h), a twin resolved from
        memory and waiting for a signature, a twin a standing rule
        covered, and the answered question asked a person again; plus a
        follow-up nobody adopted, which counts for nothing."""
        t1 = self.task("Add usage pricing", "k1")
        asked = self.node(t1)
        self.answer(asked["node_id"], "Bill it, capped at 2x.")
        self.backdate(asked["node_id"], 4)
        unchanged = self.node(t1, "Keep the 30 day retention?", "billing/retention.py")
        self.settle(t1, unchanged["node_id"], "Keep 30 days.")
        self.sign(unchanged["node_id"])
        self.backdate(unchanged["node_id"], 1)
        corrected = self.node(t1, "Round partial units up?", "billing/rounding.py")
        self.settle(t1, corrected["node_id"], "Round up.")
        self.sign(corrected["node_id"], answer="Round to the nearest unit.", rationale="fairness")
        self.backdate(corrected["node_id"], 3)
        handed = self.node(t1, "Which discount tier applies to annual plans?", "billing/discounts.py")
        self.store.refer(handed["node_id"], {"person": "Mara Ortiz", "by": "Wes Chen", "scope_kind": "none"})
        self.answer(handed["node_id"], "Tier two.")
        self.backdate(handed["node_id"], 10)
        canvas.add_followups(self.store, self.cfg, handed["node_id"], {"questions": "Does tier two need a floor?", "by": "Mara Ortiz"})
        t2 = self.task("Usage spike on the new plan", "k2")
        memory = self.node(t2)
        self.assertEqual((memory["source"], memory["signoff"], memory["authorized"]), ("memory", "required", False))
        self.store.update_settings({"auto_rules": True})
        current = self.store.get_decision(asked["node_id"])
        self.store.make_rule(asked["node_id"], {"by": "Wes Chen", "expected_updated_at": current["updated_at"]})
        t3 = self.task("Usage spike again", "k3")
        ruled = self.node(t3)
        self.assertEqual((ruled["signoff"], ruled["authorized"]), ("rule", True))
        again = self.store.add_run({"title": "Usage spike, asked in the inbox", "agent": "inbox", "repo": REPO})["id"]
        with self.store.graph.transaction():
            reasked = self.store.graph.add_decision(again, QUESTION, "", "pending", owner="Wes Chen", repo=REPO,
                                                    path="billing/usage.py")
        return {"asked": asked["node_id"], "unchanged": unchanged["node_id"], "corrected": corrected["node_id"],
                "handed": handed["node_id"], "memory": memory["node_id"], "ruled": ruled["node_id"], "reasked": reasked}


class MetricsTests(SeededStore):
    def test_every_number_is_read_from_the_seeded_ledger(self):
        self.seed()
        m = metrics.summary(self.store, days=30)
        self.assertEqual(m["window_days"], 30)
        self.assertEqual(m["decisions"], 7, "the follow-up nobody adopted is not a decision")
        self.assertEqual(m["proposals"], {"total": 4, "signed_unchanged": 1, "corrected": 1, "by_rule": 1, "open": 1,
                                          "unchanged_rate": 0.5, "corrected_rate": 0.5})
        self.assertEqual(m["time_to_answer"], {"count": 2, "median_seconds": 7 * HOUR, "p90_seconds": 10 * HOUR})
        self.assertEqual(m["time_to_signature"], {"count": 2, "median_seconds": 2 * HOUR, "p90_seconds": 3 * HOUR})
        self.assertEqual(m["reask"], {"asked": 3, "repeated": 1, "rate": 0.333})
        self.assertEqual(m["reuse"], {"total": 2, "from_memory": 2, "by_rule": 1, "duplicates": 0, "rate": 0.286})
        self.assertEqual(m["handon"]["handed_on"], 1)
        self.assertEqual(m["handon"]["rate"], round(1 / m["handon"]["routed"], 3))
        # Both reused decisions took their answer from the question Wes
        # answered in four hours: that is the wait each agent did not do.
        self.assertEqual(m["wait_saved"], {"seconds": 8 * HOUR, "decisions": 2, "estimated": 0})
        self.assertEqual([r["repo"] for r in m["by_repo"]], [REPO])
        self.assertEqual({k: v for k, v in m["by_repo"][0].items() if k != "repo"},
                         {k: v for k, v in m.items() if k not in ("window_days", "since", "until", "repo", "by_repo", "by_category", "work_proposals")})
        json.dumps(m)

    def test_objective_scope_nodes_count_by_category(self):
        # Nothing yet: both categories are still reported, empty.
        self.assertEqual([(c["category"], c["decisions"]) for c in metrics.summary(self.store)["by_category"]],
                         [("", 0), ("objective_scope", 0)])
        self.seed()
        t = self.task("Pilot to value", "k9")
        signed = self.node(t, "Prepare the CSV path as proposed?", "billing/export.py", category="objective_scope")
        self.settle(t, signed["node_id"], "Prepare it.")
        self.sign(signed["node_id"])
        corrected = self.node(t, "Prepare the importer as proposed?", "billing/import.py", category="objective_scope")
        self.settle(t, corrected["node_id"], "Prepare it.")
        self.sign(corrected["node_id"], answer="Prepare only the CSV half.", rationale="narrower")
        m = metrics.summary(self.store, days=30)
        by_category = {c["category"]: c for c in m["by_category"]}
        # The ladder categorises some task questions itself ('policy'); the two reported ones are always there.
        self.assertEqual(list(by_category), sorted(by_category))
        self.assertLessEqual({"", "objective_scope"}, set(by_category))
        scope = by_category["objective_scope"]
        self.assertEqual((scope["decisions"], scope["proposals"]["signed_unchanged"], scope["proposals"]["corrected"]), (2, 1, 1))
        self.assertEqual(scope["proposals"]["unchanged_rate"], 0.5)
        # Every decision is in exactly one category and the workspace totals keep their shape.
        self.assertEqual(sum(c["decisions"] for c in m["by_category"]), m["decisions"])
        self.assertEqual(sum(c["proposals"]["signed_unchanged"] for c in m["by_category"]), m["proposals"]["signed_unchanged"])
        self.assertEqual((m["decisions"], m["proposals"]["signed_unchanged"], m["proposals"]["corrected"]), (9, 2, 2))
        self.assertEqual(set(m["proposals"]), {"total", "signed_unchanged", "corrected", "by_rule", "open", "unchanged_rate", "corrected_rate"})

    def test_work_proposals_are_counted_from_the_ledger(self):
        self.assertEqual(metrics.summary(self.store)["work_proposals"],
                         {"created": 0, "open": 0, "signed": 0, "corrected": 0, "dismissed": 0, "precision": None,
                          "time_to_signature": {"count": 0, "median_seconds": None, "p90_seconds": None},
                          "dismissed_reasons": [], "declined_recent": []})
        # Two signed (1 h and 3 h to acceptance), one a standing rule reconciled,
        # three dismissed, one still open; and a dismissal from before the window.
        self.proposal("a", "accepted", created_hours=2, updated_hours=1)
        self.proposal("b", "accepted", created_hours=5, updated_hours=2)
        self.proposal("c", "reconcile", created_hours=6, rationale="Standing rule r1 already answers this governance question")
        self.proposal("d", "dismissed", created_hours=8, updated_hours=7, rationale="Customer already has a CSV path")
        self.proposal("e", "dismissed", created_hours=9, updated_hours=4, rationale="Customer already has a   CSV path")
        self.proposal("f", "dismissed", created_hours=10, updated_hours=3, rationale="Out of scope for the pilot; " + "x" * 80)
        self.proposal("g", "proposed", created_hours=1)
        self.proposal("h", "dismissed", created_hours=24 * 40, updated_hours=24 * 39, rationale="Too early")
        self.proposal("w", "dismissed", created_hours=1, rationale="Other repository", repo="acme/web")
        m = metrics.summary(self.store, days=30)["work_proposals"]
        self.assertEqual((m["created"], m["open"], m["signed"], m["corrected"], m["dismissed"]), (8, 2, 2, 1, 4))
        self.assertEqual(m["precision"], round(2 / 7, 3))
        self.assertEqual(m["time_to_signature"], {"count": 2, "median_seconds": 2 * HOUR, "p90_seconds": 3 * HOUR})
        self.assertEqual(m["dismissed_reasons"], [{"reason": "Customer already has a CSV path", "count": 2},
                                                  {"reason": "Other repository", "count": 1},
                                                  {"reason": ("Out of scope for the pilot; " + "x" * 80)[:60], "count": 1}])
        # What the next investigation of each repository reads: its last five dismissals, window or not.
        self.assertEqual([(d["repository"], d["title"], d["rationale"]) for d in m["declined_recent"]],
                         [(REPO, "Move f", "Out of scope for the pilot; " + "x" * 80), (REPO, "Move e", "Customer already has a   CSV path"),
                          (REPO, "Move d", "Customer already has a CSV path"), (REPO, "Move h", "Too early"), ("acme/web", "Move w", "Other repository")])
        one = metrics.summary(self.store, repo=REPO, days=30)["work_proposals"]
        self.assertEqual((one["created"], one["dismissed"], one["precision"]), (7, 3, round(2 / 6, 3)))
        self.assertEqual([d["title"] for d in one["declined_recent"]], ["Move f", "Move e", "Move d", "Move h"])
        self.assertEqual(metrics.summary(self.store, days=0)["work_proposals"]["created"], 9)
        # Nothing the proposer wrote is read: a payload claiming confidence changes no number.
        with self.store.graph.transaction() as db:
            db.execute("UPDATE objective_proposals SET payload=?", (json.dumps({"title": "Move", "confidence": "high", "precision": 1.0, "state": "accepted"}),))
        boasted = metrics.summary(self.store, days=30)["work_proposals"]
        self.assertEqual({k: v for k, v in boasted.items() if k != "declined_recent"}, {k: v for k, v in m.items() if k != "declined_recent"})
        self.assertEqual([d["rationale"] for d in boasted["declined_recent"]], [d["rationale"] for d in m["declined_recent"]])
        json.dumps(boasted)

    def test_the_window_and_the_repository_bound_what_is_counted(self):
        ids = self.seed()
        other = self.task("Pricing page copy", "k4", repo="acme/web")
        elsewhere = self.node(other, "Which price shows first?", "pricing/page.py")
        self.backdate(elsewhere["node_id"], 24 * 45)
        self.assertEqual(metrics.summary(self.store, days=30)["decisions"], 7)
        everything = metrics.summary(self.store, days=0)
        self.assertEqual(everything["decisions"], 8)
        self.assertEqual([r["repo"] for r in everything["by_repo"]], [REPO, "acme/web"])
        web = metrics.summary(self.store, repo="https://github.com/acme/web.git", days=0)
        self.assertEqual((web["repo"], web["decisions"], web["reask"]["asked"]), ("acme/web", 1, 1))
        self.assertNotIn("by_repo", web)
        self.assertEqual(metrics.summary(self.store, repo="nobody/nothing")["decisions"], 0)
        self.assertEqual(metrics.summary(self.store, repo="nobody/nothing")["time_to_answer"]["median_seconds"], None)
        # A re-ask is only one when the earlier answer existed first and
        # within ninety days of the new question.
        self.backdate(ids["reasked"], 24 * 100)
        self.assertEqual(metrics.summary(self.store, days=0)["reask"]["repeated"], 0)
        with self.assertRaises(Invalid):
            metrics.summary(self.store, days="soon")
        self.assertEqual(metrics.summary(self.store, days="")["window_days"], 30)


def load_tests(loader, tests, pattern):
    """The HTTP and tool surfaces, on SQLite only; the PostgreSQL run
    (tests/test_postgres.py) wraps MetricsTests."""
    suite = loader.loadTestsFromTestCase(MetricsTests)
    suite.addTests(loader.loadTestsFromTestCase(MetricsSurfaceTests))
    return suite


from test_auth import BOOTSTRAP, SharedServer  # noqa: E402


class MetricsSurfaceTests(SharedServer):
    def test_a_member_reads_the_numbers_over_http_and_the_status_tool_carries_them(self):
        member = self.post("/api/people", {"name": "Rosa Member", "role": "member"}, token=BOOTSTRAP)
        token = self.post("/api/tokens", {"person_id": member["id"], "kind": "human"}, token=BOOTSTRAP)["token"]
        run = self.store.add_run({"title": "Read parity", "agent": "test", "repo": REPO})["id"]
        with self.store.graph.transaction():
            self.store.graph.add_decision(run, "Which value applies?", "", "pending", owner="Rosa Member", repo=REPO)
        read = self.get("/api/metrics?days=7", token=token)
        self.assertEqual((read["window_days"], read["decisions"], read["reask"]["asked"]), (7, 1, 1))
        self.assertEqual(read["by_repo"][0]["repo"], REPO)
        self.assertEqual([c["category"] for c in read["by_category"]], ["", "objective_scope"])
        self.assertEqual(read["work_proposals"]["precision"], None)
        self.assertEqual(self.get(f"/api/metrics?repo={REPO}&days=0", token=token)["repo"], REPO)
        with self.assertRaises(HTTPError) as refused:
            self.get("/api/metrics?days=soon", token=token)
        self.assertEqual(refused.exception.code, 400)
        from bridge.mcp import call_tool
        status = call_tool(self.store, "bridge_connection_status", {})
        self.assertEqual(status["metrics"]["decisions"], 1)
        self.assertEqual(status["metrics"]["window_days"], 30)
