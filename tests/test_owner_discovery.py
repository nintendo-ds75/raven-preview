"""Provider-free bounded discovery with uniquely named synthetic contacts."""

import base64
import json
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase
from bridge import owner_discovery
from bridge.mcp import TOOLS, _list_owners, dispatch
from bridge.store import Invalid, Store, graph_summary, ownership_rows


class OwnerDiscoveryTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "discovery.db")

    def seed(self, count=3, evidence="Synthetic file history"):
        with self.store.connect() as db:
            db.executemany("INSERT INTO people(id,name,email,github_login,team,active) VALUES(?,?,?,?,?,?)",
                [(f"person-{i:05}", f"Contact{i:05} Unique{i:05}", f"contact{i:05}@synthetic.example", f"contact{i:05}", "Synthetic Squad", int(i % 5 != 4)) for i in range(count)])
            db.executemany("INSERT INTO owners(id,name,team,patterns,created_at,person_id) VALUES(?,?,?,?,?,?)",
                [(f"owner-{i:05}", f"Contact{i:05} Unique{i:05}", "Synthetic Squad", f"src/unit{i:05}/*", f"2026-01-{i+1:05}", f"person-{i:05}") for i in range(count)])
            db.executemany("INSERT INTO ownership(repo,path_prefix,engineer,source,weight,evidence) VALUES(?,?,?,?,?,?)",
                [("synthetic/alpha" if i % 2 == 0 else "synthetic/beta", f"src/unit{i:05}/", f"Contact{i:05} Unique{i:05}", "git", 1.0, evidence) for i in range(count)])
            db.executemany("INSERT INTO authority(id,repo,person_id,scope_kind,scope,role,created_at) VALUES(?,?,?,?,?,?,?)",
                [(f"authority-{i:05}", "synthetic/alpha" if i % 2 == 0 else "", f"person-{i:05}", "path", f"src/unit{i:05}/*", "knows", f"2026-01-{i+1:05}") for i in range(count)])
            db.execute("INSERT INTO teams(id,name,handle) VALUES('team-one','Synthetic Squad','synthetic/squad')")
            db.executemany("INSERT INTO team_members(team_id,person_id) VALUES('team-one',?)", [(f"person-{i:05}",) for i in range(count)])
            db.execute("INSERT INTO settings(key,value) VALUES('coordinator','person-00000')")

    def listing(self, **args):
        response = dispatch(self.store, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "bridge_list_owners", "arguments": args}})
        self.assertFalse(response["result"]["isError"], response)
        output = json.loads(response["result"]["content"][0]["text"])
        self.assertLessEqual(len(json.dumps(response["result"]["content"][0]["text"]).encode()), owner_discovery.MAX_CONTENT_BYTES)
        self.assertLess(len(json.dumps(response).encode()), 50 * 1024)
        return output

    def test_compact_legacy_values_and_new_teams_are_complete(self):
        self.seed()
        out = self.listing()
        with self.store.connect() as db:
            self.assertEqual(out["owners"], [dict(r) for r in db.execute("SELECT * FROM owners ORDER BY created_at")])
            self.assertEqual(out["ownership"], ownership_rows(db, limit=0))
            legacy_graph = graph_summary(db)
            # The old DISTINCT query has no ordering guarantee. Paging uses
            # lexical repository order so positions remain stable on both DBs.
            legacy_graph["repos"] = sorted(legacy_graph["repos"])
            self.assertEqual(out["graph"], legacy_graph)
        self.assertEqual(out["people"], [{k: p[k] for k in ("id", "name", "email", "github_login", "team", "teams", "active")} for p in self.store.people()])
        self.assertEqual(out["authority"], [{k: a[k] for k in ("id", "who", "is_team", "scope_kind", "scope", "role", "repo", "source", "asserted_by", "accepted", "effective_to")} for a in self.store.authority()])
        self.assertEqual(out["teams"], self.store.graph.teams())
        self.assertEqual(out["coordinator"], "Contact00000 Unique00000")
        self.assertFalse(out["pagination"]["truncated"])
        self.assertFalse(out["pagination"]["has_more"])
        self.assertIsNone(out["pagination"]["next_cursor"])

    def test_large_roster_pages_losslessly_and_bounds_both_wire_layers(self):
        self.seed(1311)
        # The entire real-shaped synthetic directory is deliberately retained.
        with self.store.connect() as db:
            full, graph, coordinator = owner_discovery._snapshot(db, "")
        unbounded_bytes = len(json.dumps([full, graph, coordinator]).encode())
        self.assertGreater(unbounded_bytes, 568961)
        seen = {k: [] for k in owner_discovery.COLLECTIONS}
        args = {"limit": 200}
        pages = 0
        while True:
            out = self.listing(**args)
            pages += 1
            self.assertLess(pages, 200)
            self.assertLessEqual(sum(p["returned"] for p in out["pagination"]["collections"].values()), 200)
            for key in seen:
                rows = out["graph"]["repos"] if key == "repos" else out[key]
                meta = out["pagination"]["collections"][key]
                self.assertEqual(meta["offset"], len(seen[key]))
                self.assertEqual(meta["total"], len(full[key]))
                self.assertEqual(meta["returned"], len(rows))
                seen[key].extend(rows)
            if not out["pagination"]["has_more"]:
                self.assertTrue(out["pagination"]["truncated"])
                break
            args["cursor"] = out["pagination"]["next_cursor"]
        self.assertGreater(pages, 1)
        self.assertEqual(seen, full)
        self.assertEqual(len(seen["people"]), 1311)
        self.assertEqual(len({p["id"] for p in seen["people"]}), 1311)

    def test_search_each_collection_and_keep_repository_scope_compatible(self):
        self.seed(6)
        out = self.listing(query=" UNIQUE00004 ", repo="synthetic/alpha")
        for key in ("people", "owners", "ownership", "authority"):
            self.assertEqual(len(out[key]), 1, key)
        self.assertEqual(out["people"][0]["active"], 0)
        self.assertEqual(out["people"][0]["teams"], ["Synthetic Squad"])
        self.assertEqual(out["pagination"]["collections"]["people"]["available"], 6)
        global_contacts = self.listing(repo="unknown/repo")
        self.assertEqual(len(global_contacts["people"]), 6)
        self.assertEqual(len(global_contacts["owners"]), 6)
        self.assertEqual(len(global_contacts["teams"]), 1)
        self.assertEqual(global_contacts["ownership"], [])
        self.assertEqual(len(global_contacts["authority"]), 3)
        self.assertEqual(self.listing(query="SYNTHETIC SQUAD")["people"], self.listing()["people"])
        self.assertEqual(len(self.listing(query="synthetic/squad")["teams"]), 1)
        self.assertEqual(len(self.listing(query="src/unit00002")["ownership"]), 1)
        self.assertEqual(len(self.listing(query="contact00003@synthetic.example")["people"]), 1)

    def test_no_matches_are_explicit_and_never_no_owner_or_approval_claim(self):
        self.seed()
        out = self.listing(query="no-such-synthetic-value")
        self.assertFalse(out["pagination"]["truncated"])
        self.assertEqual(out["people"], [])
        self.assertEqual(out["pagination"]["collections"]["people"]["total"], 0)
        self.assertEqual(out["pagination"]["collections"]["people"]["available"], 3)
        self.assertIn("absent rows never establish", out["notice"])
        self.assertIn("bridge_get_tree", out["notice"])
        self.assertIn("bridge_add_node", out["notice"])

    def test_cursor_binding_validation_and_deterministic_replay(self):
        self.seed()
        first = self.listing(limit=1)
        cursor = first["pagination"]["next_cursor"]
        self.assertEqual(self.listing(limit=1, cursor=cursor), self.listing(limit=1, cursor=cursor))
        for changed in ({"query": "contact"}, {"repo": "synthetic/alpha"}, {"limit": 2}):
            with self.subTest(changed=changed), self.assertRaisesRegex(Invalid, "different query, repo or limit"):
                _list_owners(self.store, {"limit": 1, "cursor": cursor, **changed})
        for bad in ("", "!", "a", "null", "x" * 2049, 1):
            with self.subTest(cursor=bad), self.assertRaisesRegex(Invalid, "Invalid owner discovery cursor"):
                _list_owners(self.store, {"cursor": bad})
        payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        for value in (-1, True, "1", 999):
            payload["offsets"]["people"] = value
            bad = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
            with self.subTest(offset=value), self.assertRaises(Invalid):
                _list_owners(self.store, {"limit": 1, "cursor": bad})

    def test_addition_or_directory_edit_invalidates_continuation(self):
        self.seed()
        for sql in ("INSERT INTO people(id,name) VALUES('added-person','ExtraUnique Contact')",
                    "UPDATE people SET team='New Synthetic Squad' WHERE id='person-00000'",
                    "UPDATE teams SET handle='synthetic/new-handle' WHERE id='team-one'",
                    "INSERT INTO team_members(team_id,person_id) VALUES('team-one','added-person')",
                    "UPDATE ownership SET evidence='New synthetic history' WHERE rowid=1"):
            cursor = self.listing(limit=1)["pagination"]["next_cursor"]
            with self.store.connect() as db:
                db.execute(sql)
            with self.subTest(sql=sql), self.assertRaisesRegex(Invalid, "results changed.*discard accumulated pages"):
                _list_owners(self.store, {"limit": 1, "cursor": cursor})
            self.listing(limit=1)

    def test_concurrent_writer_cannot_mix_a_page_snapshot(self):
        self.seed()
        def during_read(db):
            with self.store.connect() as writer:
                writer.execute("INSERT INTO people(id,name) VALUES('concurrent-person','ConcurrentUnique Contact')")
                writer.execute("INSERT INTO ownership(repo,path_prefix,engineer,source,weight,evidence) VALUES('new/repo','new/','ConcurrentUnique Contact','git',1,'new history')")
            return graph_summary(db)
        with patch.object(owner_discovery, "graph_summary", during_read):
            out = self.listing(limit=1)
        self.assertEqual(out["pagination"]["collections"]["people"]["total"], 3)
        self.assertEqual(out["pagination"]["collections"]["repos"]["total"], 2)
        with self.assertRaisesRegex(Invalid, "results changed"):
            _list_owners(self.store, {"limit": 1, "cursor": out["pagination"]["next_cursor"]})
        current = self.listing()
        self.assertEqual(len(current["people"]), 4)
        self.assertIn("new/repo", current["graph"]["repos"])

    def test_payload_budget_returns_complete_rows_and_rejects_oversized_row(self):
        self.seed(3, evidence='Unicode \u2603 "quoted" \\n' * 800)
        first = self.listing(limit=200)
        self.assertTrue(first["pagination"]["has_more"])
        self.assertLess(sum(m["returned"] for m in first["pagination"]["collections"].values()), 200)
        rows = first["ownership"][:]
        page = first
        while page["pagination"]["has_more"]:
            page = self.listing(limit=200, cursor=page["pagination"]["next_cursor"])
            rows.extend(page["ownership"])
        self.assertEqual(rows, self.store.ownership(limit=0))
        with self.store.connect() as db:
            db.execute("UPDATE ownership SET evidence=?", ("L" * 60000,))
        args = {"query": "LLLL", "limit": 200}
        with self.assertRaisesRegex(Invalid, "no part of it was returned.*authorized HTTP"):
            _list_owners(self.store, args)

    def test_graph_repositories_and_large_team_memberships_are_paged_without_clipping(self):
        self.seed(400)
        with self.store.connect() as db:
            db.executemany("INSERT INTO ownership(repo,path_prefix,engineer,source,weight,evidence) VALUES(?,'src/','GraphUnique Contact','git',1,'history')", [(f"repo/{i:04}",) for i in range(400)])
        out = self.listing(query="synthetic/squad", limit=1)
        self.assertEqual(len(out["teams"][0]["members"]), 400)
        self.assertFalse(out["pagination"]["truncated"])
        args = {"query": "repo/", "limit": 200}
        repos = []
        while True:
            out = self.listing(**args)
            repos.extend(out["graph"]["repos"])
            if not out["pagination"]["has_more"]:
                break
            args["cursor"] = out["pagination"]["next_cursor"]
        self.assertEqual(repos, [f"repo/{i:04}" for i in range(400)])

    def test_large_team_collection_and_single_row_pages_have_no_loss(self):
        self.seed(3)
        with self.store.connect() as db:
            db.executemany("INSERT INTO teams(id,name,handle) VALUES(?,?,?)", [(f"team-{i:04}", f"Squad{i:04} Unique", f"synthetic/squad{i:04}") for i in range(205)])
            full, _, _ = owner_discovery._snapshot(db, "")
        seen = {k: [] for k in owner_discovery.COLLECTIONS}
        args = {"limit": 1}
        while True:
            out = self.listing(**args)
            for key in seen:
                seen[key].extend(out["graph"]["repos"] if key == "repos" else out[key])
            if not out["pagination"]["has_more"]:
                break
            args["cursor"] = out["pagination"]["next_cursor"]
        self.assertEqual(seen, full)

    def test_filtered_continuation_detects_changed_available_directory(self):
        self.seed(3)
        cursor = self.listing(query="unique00000", limit=1)["pagination"]["next_cursor"]
        with self.store.connect() as db:
            db.execute("INSERT INTO people(id,name) VALUES('outside-query','OutsideQuery Contact')")
        with self.assertRaisesRegex(Invalid, "results changed"):
            _list_owners(self.store, {"query": "unique00000", "limit": 1, "cursor": cursor})

    def test_arguments_and_schema_are_bounded(self):
        for args in ({"limit": 0}, {"limit": 201}, {"limit": "2"}, {"limit": True}, {"query": "x" * 201}, {"repo": "x" * 501}, {"query": []}):
            with self.subTest(args=args), self.assertRaises(Invalid):
                _list_owners(self.store, args)
        spec = next(t for t in TOOLS if t["name"] == "bridge_list_owners")["inputSchema"]
        self.assertEqual(set(spec["properties"]), {"repo", "query", "limit", "cursor"})
        self.assertEqual(spec["properties"]["limit"]["maximum"], 200)
        self.assertFalse(spec["additionalProperties"])
