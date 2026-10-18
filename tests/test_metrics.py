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
from bridge.store import Invalid, Store  # noqa: E402

REPO = "acme/platform"
QUESTION = "Should we bill the usage spike?"
HOUR = 3600


class SeededStore(OfflineCase):
    """A workspace with the histories the numbers are read from, written
    through the canvas protocol and the inbox's own writes."""

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "metrics.db")
        self.cfg = Config(model_api="none")
        self.store.add_owner({"name": "Wes Chen", "team": "Data", "patterns": "billing/*"})
        self.store.add_owner({"name": "Mara Ortiz", "team": "Pricing", "patterns": "pricing/*"})

    def task(self, title, key, repo=REPO):
        return canvas.start_task(self.store, self.cfg, {"title": title, "agent": "test", "repo": repo, "client_key": key,
                                                        "paths": "billing/usage.py", "requester": "Priya"})["task_id"]

    def node(self, task, question=QUESTION, path="billing/usage.py"):
        return canvas.add_node(self.store, self.cfg, {"task_id": task, "question": question, "paths": path})

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
                         {k: v for k, v in m.items() if k not in ("window_days", "since", "until", "repo", "by_repo")})
        json.dumps(m)

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
        self.assertEqual(self.get(f"/api/metrics?repo={REPO}&days=0", token=token)["repo"], REPO)
        with self.assertRaises(HTTPError) as refused:
            self.get("/api/metrics?days=soon", token=token)
        self.assertEqual(refused.exception.code, 400)
        from bridge.mcp import call_tool
        status = call_tool(self.store, "bridge_connection_status", {})
        self.assertEqual(status["metrics"]["decisions"], 1)
        self.assertEqual(status["metrics"]["window_days"], 30)
