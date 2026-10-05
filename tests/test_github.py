"""GitHub as a source: approvals from pull request reviews land on the
squash commit that merged, a team named in CODEOWNERS resolves to its
synced members, the description and the review summaries are records,
the sync moves on a cursor and survives a rebuild of the git index, a
failure is recorded and repeated, staleness shows in the evidence, the
webhook verifies its signature and syncs one pull request."""

import copy
import hashlib
import hmac
import json
import subprocess
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.request import Request, urlopen

from fixtures import OfflineCase, git_env, run_git, workdir

from bridge.github import (GitHubError, Syncer, apply_pulls, sync_note, sync_pull, sync_repo,
                           sync_state, verify_signature)
from bridge.ingest import index_repo
from bridge.routing import route
from fixtures import ready_server as make_server
from bridge.store import Store

REPO = "acme/platform"
PRIYA = ("Priya Natarajan", "priya@acme.example")
WES = ("Wes Chen", "wes@acme.example")
DAN = ("Dan Ortiz", "dan@acme.example")


def build_platform() -> tuple[Path, str]:
    """A GitHub-flow repository: squash-merged pull requests, a team in
    CODEOWNERS, no trailers anywhere. Returns the checkout and the sha
    of the squash commit of pull request 42."""
    repo = workdir() / "platform"
    if (repo / ".built").exists():
        return repo, (repo / ".built").read_text().strip()
    repo.mkdir(exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    (repo / "billing").mkdir()
    (repo / "api").mkdir()
    (repo / "CODEOWNERS").write_text("billing/ @acme/billing-team\napi/ @dortiz\n")
    (repo / "billing" / "rates.py").write_text("RATE = 0.01\n")
    (repo / "api" / "routes.py").write_text("ROUTES = []\n")
    run_git(repo, "add", ".")
    run_git(repo, "commit", "-q", "-m", "baseline (#1)", env=git_env(DAN, 0))
    for day in range(1, 4):
        (repo / "api" / "routes.py").write_text(f"ROUTES = [{day}]\n")
        run_git(repo, "commit", "-q", "-am", f"api: route {day} (#{10 + day})", env=git_env(DAN, day))
    (repo / "billing" / "rates.py").write_text("RATE = 0.02\nTIERS = [(1000, 0.02), (10000, 0.015)]\n")
    run_git(repo, "commit", "-q", "-am", "Add tiered rates (#42)", env=git_env(PRIYA, 5))
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                         text=True).stdout.strip()
    (repo / "billing" / "rates.py").write_text("RATE = 0.02\nTIERS = [(1000, 0.02), (10000, 0.015), (100000, 0.01)]\n")
    run_git(repo, "commit", "-q", "-am", "Add a third tier (#43)", env=git_env(PRIYA, 6))
    (repo / ".built").write_text(sha)
    return repo, sha


class FakeGitHub:
    """The REST surface sync_repo touches, from a dict of routes; every
    request is recorded so a test can see what was and was not fetched."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        self.fail_on = ""

    def get(self, path, params=None):
        self.requests.append(path)
        if self.fail_on and self.fail_on in path:
            raise GitHubError(f"GitHub 500 on {path}: boom", 500)
        if path not in self.routes:
            raise GitHubError(f"GitHub 404 on {path}: Not Found", 404)
        return copy.deepcopy(self.routes[path]), {}

    def list(self, path, params=None):
        self.requests.append(path)
        if self.fail_on and self.fail_on in path:
            raise GitHubError(f"GitHub 500 on {path}: boom", 500)
        yield from copy.deepcopy(self.routes.get(path, []))


def routes_for(sha: str, sha43: str = "") -> dict:
    pr42 = {"number": 42, "title": "Add tiered rates", "updated_at": "2026-06-10T12:00:00Z",
            "merged_at": "2026-06-06T10:00:00Z", "merge_commit_sha": sha, "user": {"login": "pnatarajan"},
            "body": "Usage above the included quota is billed at tiered rates: 0.02 per unit up to 10k, "
                    "0.015 beyond. Agreed with finance; the enterprise rate card references these tiers."}
    pr41 = {"number": 41, "title": "Try a flat rate", "updated_at": "2026-06-04T12:00:00Z", "merged_at": None,
            "merge_commit_sha": None, "user": {"login": "pnatarajan"}, "body": "abandoned"}
    return {
        "/repos/acme/platform/pulls": [pr42, pr41],
        "/repos/acme/platform/pulls/42": {**pr42, "merged_by": {"login": "wchen"}},
        "/repos/acme/platform/pulls/41": {**pr41, "merged_by": None},
        "/repos/acme/platform/pulls/42/files": [{"filename": "billing/rates.py"}],
        "/repos/acme/platform/pulls/42/reviews": [
            {"id": 1, "user": {"login": "wchen"}, "state": "APPROVED", "submitted_at": "2026-06-06T09:00:00Z",
             "body": "Looks right; the tiers match the contract finance signed with Globex last quarter."},
            {"id": 2, "user": {"login": "dortiz"}, "state": "COMMENTED", "submitted_at": "2026-06-05T09:00:00Z",
             "body": "nit"}],
        "/orgs/acme/teams/billing-team": {"name": "Billing Team"},
        "/orgs/acme/teams/billing-team/members": [{"login": "pnatarajan"}, {"login": "wchen"}, {"login": "newbie"}],
        "/users/newbie": {"name": "New Person", "email": "new@acme.example"},
        "/users/dortiz": {"name": "Dan Ortiz", "email": ""},
    }


class GitHubCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.repo, self.sha = build_platform()
        self.store = Store(Path(self.temp.name) / "gh.db")
        self.graph = self.store.graph
        index_repo(self.graph, self.repo, repo_name=REPO)
        with self.graph.transaction():
            self.priya = self.graph.add_person(*PRIYA, github_login="pnatarajan")
            self.wes = self.graph.add_person(*WES, github_login="wchen")
        self.api = FakeGitHub(routes_for(self.sha))

    def approvals(self):
        return sorted((r["engineer"], r["role"]) for r in self.graph.changes_under(REPO, "billing/")
                      if r["sha"] == self.sha and r["role"] != "author")


class SyncTests(GitHubCase):
    def test_approvals_land_on_the_squash_commit_and_route(self):
        self.assertEqual(self.approvals(), [])
        before = route(self.graph, REPO, "Should the tiers apply to partner accounts?", path="billing/rates.py",
                       requester=PRIYA[0])
        self.assertTrue(before is None or before[0] != WES[0])
        stats = sync_repo(self.graph, self.api, REPO)
        self.assertEqual(stats["pulls"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["approvals"], 1)
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        # The author git recorded is not written twice under another name.
        authors = [r["engineer"] for r in self.graph.changes_under(REPO, "billing/") if r["sha"] == self.sha and r["role"] == "author"]
        self.assertEqual(authors, [PRIYA[0]])
        after = route(self.graph, REPO, "Should the tiers apply to partner accounts?", path="billing/rates.py",
                      requester=PRIYA[0])
        self.assertIsNotNone(after)
        self.assertEqual(after[0], WES[0])
        self.assertTrue(any("Approved-by" in line for line in after[1]), after[1])

    def test_team_codeowners_resolve_to_synced_members(self):
        self.assertEqual(self.graph.team_members_by_handle("acme/billing-team"), [])
        sync_repo(self.graph, self.api, REPO)
        members = sorted(p["name"] for p in self.graph.team_members_by_handle("acme/billing-team"))
        self.assertEqual(members, ["New Person", PRIYA[0], WES[0]])
        newbie = next(p for p in self.graph.people() if p["github_login"] == "newbie")
        self.assertEqual(newbie["source"], "github")
        self.assertEqual(newbie["email"], "new@acme.example")
        team = next(t for t in self.graph.teams() if t["handle"].endswith("billing-team"))
        self.assertEqual(team["name"], "Billing Team")
        ranked = route(self.graph, REPO, "Should the tiers apply to partner accounts?", path="billing/rates.py",
                       requester=DAN[0])
        self.assertTrue(any("member" in line for line in ranked[1]), ranked[1])
        # Someone leaves the team: the next sync drops them, and says so.
        self.api.routes["/orgs/acme/teams/billing-team/members"] = [{"login": "pnatarajan"}, {"login": "wchen"}]
        sync_repo(self.graph, self.api, REPO)
        members = sorted(p["name"] for p in self.graph.team_members_by_handle("acme/billing-team"))
        self.assertEqual(members, [PRIYA[0], WES[0]])
        removed = [json.loads(r["detail"]) for r in self.graph.db.execute(
            "SELECT detail FROM events WHERE kind='team_synced' ORDER BY id")]
        self.assertEqual(removed[-1]["removed"], [newbie["id"]])

    def test_the_description_and_the_review_are_records(self):
        sync_repo(self.graph, self.api, REPO)
        pr = self.graph.db.execute("SELECT * FROM intents WHERE repo=? AND kind='pr' AND ref='42'", (REPO,)).fetchone()
        self.assertIn("tiered rates", pr["body"])
        self.assertEqual(pr["author"], PRIYA[0])
        self.assertEqual(pr["status"], "merged")
        review = self.graph.db.execute("SELECT * FROM intents WHERE repo=? AND kind='review'", (REPO,)).fetchall()
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["ref"], "42:1")
        self.assertEqual(review[0]["author"], WES[0])
        self.assertIn("Globex", review[0]["body"])
        paths = self.graph.paths_of_intents(REPO, [("pr", "42"), ("review", "42:1")])
        self.assertEqual(paths[("pr", "42")], ["billing/rates.py"])
        self.assertEqual(paths[("review", "42:1")], ["billing/rates.py"])
        found = self.store.search("tiered rates above the included quota", repo=REPO)
        self.assertTrue(isinstance(found["matches"], list))

    def test_sync_is_incremental_and_survives_a_rebuild(self):
        sync_repo(self.graph, self.api, REPO)
        state = sync_state(self.graph, REPO)
        self.assertEqual(state["cursor"], "2026-06-10T12:00:00Z")
        self.assertTrue(state["last_success_at"])
        self.assertEqual(state["last_error"], "")
        self.api.requests.clear()
        # A second poll re-reads the overlap window before the watermark
        # (idempotent writes): the pull request at the watermark's own
        # second is read again rather than left behind.
        again = sync_repo(self.graph, self.api, REPO)
        self.assertEqual(again["pulls"], 1)
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        # A newer pull request lands: it is fetched, and the watermark moves.
        sha43 = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True, capture_output=True,
                               text=True).stdout.strip()
        pr43 = {"number": 43, "title": "Add a third tier", "updated_at": "2026-06-12T12:00:00Z",
                "merged_at": "2026-06-07T10:00:00Z", "merge_commit_sha": sha43, "user": {"login": "pnatarajan"},
                "body": "A third tier at 0.01 beyond 100k units, as agreed with finance for enterprise volume.",
                "merged_by": {"login": "wchen"}}
        self.api.routes["/repos/acme/platform/pulls"].insert(0, pr43)
        self.api.routes["/repos/acme/platform/pulls/43/files"] = [{"filename": "billing/rates.py"}]
        self.api.routes["/repos/acme/platform/pulls/43/reviews"] = [
            {"id": 7, "user": {"login": "wchen"}, "state": "APPROVED", "submitted_at": "2026-06-07T09:00:00Z", "body": ""}]
        self.api.requests.clear()
        third = sync_repo(self.graph, self.api, REPO)
        self.assertEqual(third["pulls"], 2)
        self.assertIn("/repos/acme/platform/pulls/43/reviews", self.api.requests)
        self.assertEqual(sync_state(self.graph, REPO)["cursor"], "2026-06-12T12:00:00Z")
        # Now pull request 42 is older than the overlap window: left alone.
        self.api.requests.clear()
        fourth = sync_repo(self.graph, self.api, REPO)
        self.assertEqual(fourth["pulls"], 1)
        self.assertNotIn("/repos/acme/platform/pulls/42/reviews", self.api.requests)
        self.assertIn("/repos/acme/platform/pulls/43/reviews", self.api.requests)
        # A rebuild of the git index drops nothing GitHub said.
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        stats = index_repo(self.graph, self.repo, repo_name=REPO)
        self.assertEqual(stats["github"]["pulls"], 2)
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        rows = [r for r in self.graph.changes_under(REPO, "billing/") if r["sha"] == sha43 and r["role"] == "approved-by"]
        self.assertEqual([r["engineer"] for r in rows], [WES[0]])
        self.assertEqual(apply_pulls(self.graph, REPO)["pulls"], 2)

    def test_a_failed_sync_is_recorded_and_repeated(self):
        self.api.fail_on = "/reviews"
        with self.assertRaises(GitHubError):
            sync_repo(self.graph, self.api, REPO)
        state = sync_state(self.graph, REPO)
        self.assertIn("boom", state["last_error"])
        self.assertEqual(state["cursor"], "")
        self.assertEqual(state["last_success_at"], "")
        self.assertTrue(state["last_attempt_at"])
        self.assertEqual(self.approvals(), [])
        self.api.fail_on = ""
        sync_repo(self.graph, self.api, REPO)
        state = sync_state(self.graph, REPO)
        self.assertEqual(state["last_error"], "")
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])

    def test_staleness_shows_in_the_evidence(self):
        self.assertIn("not synced", sync_note(self.graph, REPO))
        self.assertEqual(sync_note(self.graph, "platform"), "")
        notes = []
        ranked = route(self.graph, REPO, "Should the tiers apply to partner accounts?", path="billing/rates.py", notes=notes)
        self.assertTrue(any("GitHub not synced" in n for n in notes), notes)
        # The operator's instructions go to the agent's notes and to
        # bridge_connection_status. The line on the evidence, which a
        # person reads under "Why you" in Slack, says only what is known.
        self.assertTrue(any("GITHUB_TOKEN" in n for n in notes), notes)
        evidence = " | ".join(ranked[1])
        self.assertIn("GitHub not synced", evidence)
        self.assertNotIn("GITHUB_TOKEN", evidence)
        self.assertNotIn("bridge sync", evidence)
        from bridge.mcp import _github_status
        status = _github_status(self.store)
        self.assertEqual(status["synced"], [])
        self.assertIn(REPO, [r["repo"] for r in status["not_synced"]])
        self.assertIn("GITHUB_TOKEN", status["how"])
        sync_repo(self.graph, self.api, REPO)
        self.assertEqual(sync_note(self.graph, REPO), "")
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with self.graph.transaction():
            self.graph.db.execute("UPDATE sync_state SET last_success_at=? WHERE repo=?", (old, REPO))
        self.assertIn("10 days ago", sync_note(self.graph, REPO))
        ranked = route(self.graph, REPO, "Should the tiers apply to partner accounts?", path="billing/rates.py",
                       requester=PRIYA[0])
        self.assertTrue(any("10 days ago" in line for line in ranked[1]), ranked[1])

    def test_the_scheduler_registers_and_ticks(self):
        syncer = Syncer(self.graph, self.api, repos=[REPO], minutes=1)
        self.assertEqual([s["repo"] for s in __import__("bridge.github", fromlist=["sync_states"]).sync_states(self.graph)], [REPO])
        results = syncer.tick()
        self.assertEqual(results[0]["pulls"], 1)
        self.assertTrue(sync_state(self.graph, REPO)["last_success_at"])
        self.api.fail_on = "/pulls"
        self.api.routes["/repos/acme/platform/pulls"][0]["updated_at"] = "2026-07-01T00:00:00Z"
        self.assertEqual(syncer.tick(), [])
        self.assertIn("boom", sync_state(self.graph, REPO)["last_error"])
        syncer.start()
        syncer.close()

    def test_one_pull_request_from_a_webhook(self):
        result = sync_pull(self.graph, self.api, REPO, 42)
        self.assertTrue(result["merged"])
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        # One object through the webhook is not a complete poll: the
        # watermark stays where the last complete poll left it.
        state = sync_state(self.graph, REPO)
        self.assertEqual(state["cursor"], "")
        self.assertEqual(state["stats"]["webhook_number"], 42)
        self.assertEqual(sync_pull(self.graph, self.api, REPO, 41)["merged"], False)


class WebhookTests(GitHubCase):
    SECRET = "s3cret"

    def setUp(self):
        super().setUp()
        self.server = make_server(self.store, port=0, github_webhook_secret=self.SECRET, github_api=self.api)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(close)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, event, payload, delivery="d1", sign=True):
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
        if sign:
            headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(self.SECRET.encode(), body, hashlib.sha256).hexdigest()
        try:
            with urlopen(Request(self.url + "/webhooks/github", data=body, headers=headers)) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode() or "{}")

    def test_signature_then_sync(self):
        self.assertFalse(verify_signature(self.SECRET, b"x", ""))
        status, body = self.post("ping", {"zen": "Keep it logically awesome."}, sign=False)
        self.assertEqual(status, 401)
        status, body = self.post("ping", {"zen": "Keep it logically awesome."})
        self.assertEqual((status, body.get("pong")), (200, True))
        merged = {"action": "closed", "repository": {"full_name": "acme/platform"},
                  "pull_request": {"number": 42, "merged": True}}
        status, body = self.post("pull_request", merged, delivery="d2")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["merged"])
        self.assertEqual(self.approvals(), [(WES[0], "approved-by")])
        status, body = self.post("pull_request", merged, delivery="d2")
        self.assertTrue(body.get("duplicate"))
        status, body = self.post("pull_request", {**merged, "action": "opened"}, delivery="d3")
        self.assertEqual(body.get("ignored"), "opened")
        status, body = self.post("pull_request_review", {"action": "submitted", "repository": {"full_name": "acme/platform"},
                                                          "pull_request": {"number": 42}}, delivery="d4")
        self.assertEqual(status, 200)
        status, body = self.post("membership", {"action": "added", "organization": {"login": "acme"}}, delivery="d5")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(p["name"] for p in self.graph.team_members_by_handle("acme/billing-team")),
                         ["New Person", PRIYA[0], WES[0]])
        with urlopen(self.url + "/api/sync") as resp:
            listed = json.load(resp)
        self.assertEqual([s["repo"] for s in listed["sync"]], [REPO])
        self.assertTrue(listed["github"])

    def test_a_delivery_that_failed_is_handled_when_github_sends_it_again(self):
        """Measured live: a merged pull request whose sync failed was
        answered 502, and GitHub's redelivery was ignored as a duplicate."""
        merged = {"action": "closed", "repository": {"full_name": "acme/platform"},
                  "pull_request": {"number": 42, "merged": True}}
        with patch("bridge.github.sync_pull", side_effect=GitHubError("GitHub 502 on /repos/acme/platform/pulls/42")):
            status, body = self.post("pull_request", merged, delivery="retry-me")
        self.assertEqual(status, 502, body)
        status, body = self.post("pull_request", merged, delivery="retry-me")
        self.assertEqual(status, 200, body)
        self.assertFalse(body.get("duplicate"))
        self.assertTrue(body["merged"])
        status, body = self.post("pull_request", merged, delivery="retry-me")
        self.assertTrue(body.get("duplicate"))


    def test_a_delivery_before_the_token_is_set_is_handled_when_resent_after(self):
        """With no GITHUB_TOKEN nothing is fetched, and a delivery kept as
        received would make GitHub's redelivery, once the token is set, a
        duplicate of a delivery that did nothing."""
        from bridge.github import handle_webhook
        merged = {"action": "closed", "repository": {"full_name": "acme/platform"},
                  "pull_request": {"number": 42, "merged": True}}
        first = handle_webhook(self.graph, None, "pull_request", "before-token", merged)
        self.assertFalse(first["ok"])
        self.assertIn("redeliver this event", first["error"])
        again = handle_webhook(self.graph, self.api, "pull_request", "before-token", merged)
        self.assertFalse(again.get("duplicate"))
        self.assertTrue(again["merged"])
        self.assertTrue(handle_webhook(self.graph, self.api, "pull_request", "before-token", merged).get("duplicate"))
        # A ping needs no token and is answered either way.
        self.assertTrue(handle_webhook(self.graph, None, "ping", "ping-1", {})["pong"])

if __name__ == "__main__":
    unittest.main()
