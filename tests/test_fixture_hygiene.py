"""No real people anywhere: fixtures, evidence, docs and examples use
invented names and reserved example domains, and the evaluations that
replay public repositories pseudonymize every identity they ingest
before anything is routed or recorded."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import MAINTAINERS, NODE_PEOPLE, PEOPLE, ROOT, OfflineCase  # noqa: E402

from evals import pseudonyms  # noqa: E402

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_BINARY = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".woff", ".woff2", ".pdf")


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
            text = path.read_text(errors="replace")
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


class PseudonymTests(OfflineCase):
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
