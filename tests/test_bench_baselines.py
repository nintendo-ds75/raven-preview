"""The routing bench's baselines and its separate rates, on items built
by hand: each baseline routes on one signal, each is scored with the
same metrics as Bridge, and the strata sort items by what makes them
hard."""

import unittest

from bench.routing.baselines import baseline_outcomes, codeowners_only, manual_map, most_recent_reviewer
from bench.routing.metrics import score, stratify, summarize, summarize_strata


def item(**overrides) -> dict:
    base = {
        "dataset": "t", "idx": 0, "sha": "abc", "base": "abb", "ts": "2026-06-10T10:00:00+00:00",
        "author": {"name": "Daniel Barboza", "email": "daniel@ventana.example", "handle": ""},
        "subject": "riscv: add the imsic compatible string", "body": "",
        "files": ["hw/riscv/virt.c"],
        "strong": [{"name": "Alistair Francis", "email": "alistair@riscv.example", "handle": ""}],
        "listing": [{"name": "Alistair Francis", "email": "alistair@riscv.example", "handle": ""}],
        "teams": [], "label_sources": {"Reviewed-by": ["Alistair Francis"], "MAINTAINERS": ["Alistair Francis"]},
        "listing_patterns": ["hw/riscv/"],
        "related": [
            {"name": "Alistair Francis", "email": "alistair@riscv.example", "roles": ["reviewed-by", "author"],
             "last_ts": "2026-06-01T10:00:00+00:00"},
            {"name": "Peter Maydell", "email": "peter@integrator.example", "roles": ["committer"],
             "last_ts": "2026-06-08T10:00:00+00:00"},
            {"name": "Daniel Barboza", "email": "daniel@ventana.example", "roles": ["author"],
             "last_ts": "2026-06-09T10:00:00+00:00"}],
    }
    base.update(overrides)
    return base


class BaselineTests(unittest.TestCase):
    def test_each_baseline_routes_on_one_signal(self):
        it = item()
        listed = codeowners_only(it, "Who should approve this change?", "")
        self.assertEqual(listed["owner"], "Alistair Francis")
        self.assertIn("MAINTAINERS lists Alistair Francis", listed["evidence"][0])
        recent = most_recent_reviewer(it)
        self.assertEqual(recent["owner"], "Peter Maydell")
        self.assertIn("most recent reviewer or acceptor in the touched directories (2026-06-08)", recent["evidence"][0])
        mapped = manual_map(it, {"hw/riscv": "Alistair Francis", "hw": "Peter Maydell"})
        self.assertEqual(mapped["owner"], "Alistair Francis")
        self.assertEqual(manual_map(it, {"net": "Jason Wang"})["owner"], "")
        nobody = codeowners_only(item(listing=[], teams=["qemu/riscv"]))
        self.assertEqual(nobody["owner"], "")
        outcomes = baseline_outcomes(it, "q", "c", mapping={"hw/riscv": "Alistair Francis"})
        self.assertEqual(sorted(outcomes), ["baseline-codeowners", "baseline-manual-map", "baseline-recent-reviewer"])
        self.assertEqual(baseline_outcomes(it, "q", "c", mapping={}).keys(), {"baseline-codeowners", "baseline-recent-reviewer"})

    def test_baselines_are_scored_like_bridge(self):
        it = item()
        scored = {k: score(it, v) for k, v in baseline_outcomes(it, "q", "c", mapping={}).items()}
        self.assertTrue(scored["baseline-codeowners"]["hit1"])
        self.assertEqual(scored["baseline-codeowners"]["evidence_why"], "listing")
        self.assertFalse(scored["baseline-recent-reviewer"]["hit1"])
        self.assertEqual(scored["baseline-recent-reviewer"]["mechanism"], "plausible_reviewer_not_labeled")
        summary = summarize(list(scored.values()))
        self.assertEqual(summary["coverage"], 1.0)
        self.assertEqual(summary["precision_routed"], 0.5)
        self.assertEqual(summary["acceptable_routed"], 0.5)
        self.assertEqual(summary["authorized_signer"], 0.5)

    def test_strata_sort_items_by_what_makes_them_hard(self):
        self.assertEqual(stratify(item()), [])
        self.assertIn("team_only_listing", stratify(item(listing=[], teams=["qemu/riscv"])))
        self.assertIn("no_trailers", stratify(item(label_sources={"committer": ["Peter Maydell"]},
                                                   strong=[{"name": "Peter Maydell", "email": "peter@integrator.example", "handle": ""}])))
        self.assertIn("new_files", stratify(item(related=[])))
        self.assertIn("stale_listing", stratify(item(related=[{"name": "Alistair Francis", "email": "alistair@riscv.example",
                                                                 "roles": ["author"], "last_ts": "2024-01-01T00:00:00+00:00"}])))
        aliased = item(strong=[{"name": "A. Francis", "email": "alistair@riscv.example", "handle": ""}])
        self.assertIn("aliases", stratify(aliased))
        rows = [{"item": item(listing=[], teams=["qemu/riscv"]), "scores": {"warm": score(item(), {"owner": "Alistair Francis", "evidence": ["MAINTAINERS lists Alistair Francis for hw/riscv/"]})}},
                {"item": item(), "scores": {"warm": score(item(), {"owner": "", "evidence": []})}}]
        strata = summarize_strata(rows, "warm")
        self.assertEqual(strata["team_only_listing"]["n"], 1)
        self.assertEqual(strata["team_only_listing"]["hit1"], 1.0)
        self.assertNotIn("new_files", strata)


if __name__ == "__main__":
    unittest.main()
