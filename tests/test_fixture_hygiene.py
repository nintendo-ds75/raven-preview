"""No real people anywhere: fixtures, evidence, docs and examples use
invented names and reserved example domains, and the evaluations that
replay public repositories pseudonymize every identity they ingest
before anything is routed or recorded."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import MAINTAINERS, NODE_PEOPLE, PEOPLE, ROOT, OfflineCase, run_git  # noqa: E402

from evals import pseudonyms  # noqa: E402

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9])")
_BINARY = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".woff", ".woff2", ".pdf")


def _scan_text(path):
    text = path.read_text(errors='replace')
    if path.suffix not in ('.json', '.jsonl'):
        return text
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, item in value.items():
                yield key
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
    try:
        values = [json.loads(line) for line in text.splitlines() if line.strip()] if path.suffix == '.jsonl' else [json.loads(text)]
        return '\n'.join(strings(values))
    except ValueError:
        return text


def _reserved(address: str, domain: str) -> bool:
    """RFC 2606 and RFC 6761 names, plus the GitHub service addresses a
    squash merge leaves in history."""
    domain = domain.lower()
    return (domain.endswith((".example", ".invalid", ".test", ".local", ".localhost"))
            or domain in ("example.com", "example.org", "example.net", "users.noreply.github.com")
            or address.lower() in ("noreply@github.com", "git@github.com"))


@unittest.skipUnless((ROOT / ".git").exists(), "requires a Git checkout")
class TrackedFilesTests(unittest.TestCase):
    def test_every_address_in_the_repository_uses_a_reserved_domain(self):
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
        offenders: dict[str, set[str]] = {}
        for rel in tracked.stdout.split():
            if rel.endswith(_BINARY):
                continue
            path = ROOT / rel
            if not path.is_file():
                continue
            # Decode transcript strings first: JSON's literal \\n before a
            # Python @decorator otherwise looks like an email local part.
            text = _scan_text(path)
            for m in _EMAIL.finditer(text):
                if not _reserved(m.group(0), m.group(1)):
                    offenders.setdefault(rel, set()).add(m.group(0))
        self.assertEqual(offenders, {}, f"addresses outside reserved domains: {offenders}")

    def test_fixture_people_live_on_example_domains(self):
        for name, address in list(PEOPLE.values()) + list(NODE_PEOPLE.values()):
            if "[bot]" in name:
                continue
            domain = address.split("@", 1)[1]
            self.assertTrue(_reserved(address, domain), (name, address))
        for address in _EMAIL.findall(MAINTAINERS):
            self.assertTrue(address.endswith(".example"), address)


class SerializedEvidenceTests(OfflineCase):
    def test_json_newline_before_decorator_is_not_an_email(self):
        path = Path(self.temp.name) / 'transcript.jsonl'
        path.write_text(json.dumps({'code': '@pytest.fixture\ndef case():\n    @pytest.mark.parametrize', 'author': 'someone@example.invalid'}) + '\n')
        self.assertEqual([m.group(0) for m in _EMAIL.finditer(_scan_text(path))], ['someone@example.invalid'])


class PseudonymTests(OfflineCase):
    def test_aliases_that_share_a_pseudonym_do_not_break_ingestion(self):
        store = self.fresh_store('aliases.db')
        self.addCleanup(store.graph.close)
        store.graph.db.execute("INSERT INTO engineers(id,name,email) VALUES('one','Ada M. Example','Ada@example.invalid')")
        store.graph.db.execute("INSERT INTO engineers(id,name,email) VALUES('two','Ada Example','ada@example.invalid')")
        pseudonyms.pseudonymize(store.graph)
        rows = store.graph.db.execute('SELECT * FROM engineers').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], pseudonyms.label('Ada Example'))
        pseudonyms.pseudonymize(store.graph)
        self.assertEqual(store.graph.db.execute('SELECT count(*) n FROM engineers').fetchone()['n'], 1)

    def test_source_versions_and_search_survive_pseudonymization(self):
        import json
        from bridge import context_memory as cm
        store = self.warm_store('qemulike')
        g = store.graph
        source = store.add_record({'repo': 'qemulike', 'kind': 'doc', 'ref': 'pseudonym-policy',
            'title': 'Example review policy', 'body': PEOPLE['oriel'][0] + ' approved this proposal.',
            'author': PEOPLE['oriel'][0], 'paths': ['hw/riscv/virt.c']})
        before = g.db.execute('SELECT count(*) FROM source_versions').fetchone()[0]
        pseudonyms.pseudonymize(g)
        row = g.db.execute('SELECT * FROM source_versions WHERE id=?',
                          (source['source']['source_version_id'],)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(g.db.execute('SELECT count(*) FROM source_versions').fetchone()[0], before)
        snap = json.loads(row['snapshot'])
        self.assertEqual(snap['author'], pseudonyms.label(PEOPLE['oriel'][0]))
        self.assertNotIn(PEOPLE['oriel'][0], snap['body'])
        self.assertEqual(row['fingerprint'], cm.digest({k: v for k, v in snap.items()
            if k not in ('source_updated_at', 'source_version', 'source_sequence')}))
        self.assertTrue(g._fts_ids('intents_fts', ['proposal']))
        pseudonyms.pseudonymize(g)
        self.assertEqual(g.db.execute('SELECT snapshot FROM source_versions WHERE id=?',
            (row['id'],)).fetchone()[0], row['snapshot'])

    def test_refuses_to_rewrite_decision_history(self):
        store = self.warm_store('qemulike')
        task = store.add_run({'title': 'An evaluation already started'})['id']
        store.graph.add_decision(task, 'Keep current behavior?', 'policy', 'pending')
        with self.assertRaisesRegex(ValueError, 'before creating decisions'):
            pseudonyms.pseudonymize(store.graph)

    def test_labels_are_stable_across_spellings_and_keep_their_links(self):
        self.assertEqual(pseudonyms.label("Rafaela M. Núñez"), pseudonyms.label("rafaela nunez"))
        self.assertNotEqual(pseudonyms.label("Rafaela Nunez"), pseudonyms.label("Rafaela Vance"))
        self.assertTrue(pseudonyms.label("Oriel Vance").startswith("Engineer "))
        self.assertEqual(pseudonyms.email("Oriel@RISCV.example"), pseudonyms.email("oriel@riscv.example"))
        self.assertTrue(pseudonyms.email("oriel@riscv.example").endswith("@example.invalid"))
        # A noreply address still carries the (pseudonymous) login the
        # CODEOWNERS handle resolves to.
        self.assertEqual(pseudonyms.email("1234+codhiambo@users.noreply.github.com"),
                         pseudonyms.handle("codhiambo") + "@users.noreply.github.com")
        self.assertEqual(pseudonyms.listed("@codhiambo"), "@" + pseudonyms.handle("codhiambo"))
        self.assertEqual(pseudonyms.listed("@nodejs/sqlite"), "@nodejs/sqlite")
        self.assertEqual(pseudonyms.person("dependabot[bot]", "bot@example"), ("dependabot[bot]", "bot@example", ""))
        self.assertEqual(pseudonyms.person("GitHub", "noreply@github.com"), ("GitHub", "noreply@github.com", ""))

    def test_the_graph_keeps_no_ingested_name_and_routes_the_same(self):
        from bridge.routing import route
        store = self.warm_store("qemulike")
        g = store.graph
        question = "Should hw/riscv/virt.c add the imsics compatible string?"
        before = route(g, "qemulike", question)
        self.assertEqual(before[0], PEOPLE["oriel"][0])
        scrub = pseudonyms.pseudonymize(g)
        self.assertGreater(scrub.counts["engineers"], 0)
        self.assertGreater(scrub.counts["change_people"], 0)
        # The scrubber learned the identities as they were, for text the
        # caller still holds about them.
        self.assertEqual(scrub.scrub(f"{PEOPLE['oriel'][0]} merged it"), f"{pseudonyms.label(PEOPLE['oriel'][0])} merged it")
        # Bots are services, not people, and stay as they are.
        real = {name for name, _ in PEOPLE.values() if "[bot]" not in name}
        real |= {address for name, address in PEOPLE.values() if "[bot]" not in name}
        for table, column in (("engineers", "name"), ("engineers", "email"), ("change_people", "engineer"),
                              ("change_people", "email"), ("blame_lines", "engineer"), ("listings", "person"),
                              ("listings", "email"), ("ownership", "engineer")):
            values = {r[0] for r in g.db.execute(f"SELECT {column} FROM {table}").fetchall()}
            self.assertFalse(values & real, (table, column, values & real))
        after = route(g, "qemulike", question)
        self.assertEqual(after[0], pseudonyms.label(PEOPLE["oriel"][0]), after[1])
        self.assertIn(f"MAINTAINERS lists {pseudonyms.label(PEOPLE['oriel'][0])}", " | ".join(after[1]))
        net = route(g, "qemulike", "Should hw/net/virtio-net.c drop the legacy header?")
        self.assertEqual(net[0], pseudonyms.label(PEOPLE["kwame"][0]), net[1])

    def test_codeowners_logins_keep_their_noreply_link_after_pseudonymizing(self):
        """A CODEOWNERS entry is a bare login. Raven joins it to the person
        whose commits carry that login in a GitHub noreply address; the
        pseudonyms must move both sides of that join together."""
        from bridge.ingest import index_repo
        from bridge.routing import route
        repo = Path(self.temp.name) / "ownedco"
        (repo / "billing").mkdir(parents=True)
        run_git(repo, "init", "-q", "-b", "main")
        (repo / "CODEOWNERS").write_text("billing/ @codhiambo\n")
        owner = ("Cyrus Odhiambo", "1234+codhiambo@users.noreply.github.com")
        other = ("Wanjiru Kamau", "wanjiru@owned.example")
        for day, (who, name) in enumerate([(owner, "meter.py"), (other, "meter.py"), (other, "invoice.py"), (owner, "pricing.py")]):
            path = repo / "billing" / name
            path.write_text((path.read_text() if path.exists() else "") + f"# change {day}\n")
            stamp = f"2024-01-{day + 1:02d}T12:00:00+00:00"
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", f"billing: change {day}", env={
                "GIT_AUTHOR_NAME": who[0], "GIT_AUTHOR_EMAIL": who[1], "GIT_AUTHOR_DATE": stamp,
                "GIT_COMMITTER_NAME": who[0], "GIT_COMMITTER_EMAIL": who[1], "GIT_COMMITTER_DATE": stamp})
        store = self.fresh_store("ownedco.db")
        index_repo(store.graph, repo)
        question = "Should billing/meter.py round usage up to the next unit?"
        before = route(store.graph, "ownedco", question, path="billing/meter.py")
        self.assertEqual(before[0], owner[0], before)
        self.assertIn(f"CODEOWNERS lists {owner[0]} for billing/", " | ".join(before[1]))
        pseudonyms.pseudonymize(store.graph)
        after = route(store.graph, "ownedco", question, path="billing/meter.py")
        self.assertEqual(after[0], pseudonyms.label(owner[0]), after)
        self.assertIn(f"CODEOWNERS lists {pseudonyms.label(owner[0])} for billing/", " | ".join(after[1]))
        listed = {r[0] for r in store.graph.db.execute("SELECT person FROM listings WHERE kind='codeowners'")}
        self.assertEqual(listed, {pseudonyms.handle("codhiambo")})

    def test_a_simulated_directory_lists_pseudonymous_members_only(self):
        store = self.warm_store("qemulike")
        pseudonyms.pseudonymize(store.graph)
        members = pseudonyms.members_from_graph(store.graph)
        self.assertTrue(members)
        for m in members:
            self.assertTrue(m["real_name"].startswith("Engineer "), m)
            self.assertTrue(m["profile"]["email"].endswith("@example.invalid"), m)
        scrub = pseudonyms.learn_graph(store.graph)
        self.assertEqual(scrub.scrub("ask Engineer nobody"), "ask Engineer nobody")

    def test_the_scrubber_rewrites_names_addresses_and_handles_in_prose(self):
        s = pseudonyms.Scrubber()
        s.learn("Oriel Vance", "oriel@riscv.example")
        s.learn(login="codhiambo")
        text = "cc @codhiambo, Oriel Vance <oriel@riscv.example> reviewed; oriel vance agreed."
        out = s.scrub(text)
        self.assertNotIn("Oriel", out)
        self.assertNotIn("oriel@riscv.example", out)
        self.assertNotIn("@codhiambo", out)
        self.assertIn(pseudonyms.label("Oriel Vance"), out)
        self.assertIn("@" + pseudonyms.handle("codhiambo"), out)
        self.assertIn(pseudonyms.email("oriel@riscv.example"), out)


if __name__ == "__main__":
    unittest.main()
