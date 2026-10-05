"""The end-to-end evaluation runs the example task file under all three
arms and the arms come out the way the loop is sold: Bridge asks the
right person first, never interrupts anyone about a decision a rule
covers, acts only on authorized answers and follows them; routing alone
interrupts; a host alone follows its own defaults."""

import json
import tempfile
import unittest
from pathlib import Path

from fixtures import OfflineCase

from evals.e2e.run import ARMS, load_tasks, run_suite, write_report

EXAMPLE = Path(__file__).resolve().parents[1] / "evals" / "e2e" / "tasks.example.json"


class EvalTests(OfflineCase):
    def test_the_three_arms_on_the_example_tasks(self):
        spec = load_tasks(EXAMPLE)
        results = run_suite(spec, ARMS, db_dir=self.temp.name)
        by = {(r["task"], r["arm"]): r for r in results["tasks"]}
        bridge = results["summary"]["bridge"]
        self.assertEqual(bridge["interruptions"], 0)
        self.assertEqual(bridge["first_contact_rate"], 1.0)
        self.assertEqual(bridge["authorized_rate"], 1.0)
        self.assertEqual(bridge["adherence_rate"], 1.0)
        self.assertEqual(bridge["lost_rate"], 0.0)
        self.assertEqual(bridge["discovered"], 3)
        self.assertEqual(bridge["asks"], 2)
        self.assertTrue(any("authorized without asking" in n for n in by[("overage-pricing", "bridge")]["notes"]))
        routing = results["summary"]["routing-only"]
        self.assertEqual(routing["interruptions"], 1)
        self.assertEqual(routing["asks"], 3)
        self.assertEqual(routing["authorized_rate"], 0.0)
        host = results["summary"]["host-alone"]
        self.assertEqual(host["asks"], 0)
        self.assertEqual(host["discovered"], 0)
        self.assertEqual(host["adherence_rate"], 0.0)
        with tempfile.TemporaryDirectory() as tmp:
            text = write_report(results, Path(tmp) / "out")
            self.assertIn("| bridge | 2 | 3 | 2 | 0 | 100% | 100% | 100% | 0% |", text)
            written = json.loads((Path(tmp) / "out" / "results.json").read_text())
            self.assertEqual(written["summary"]["bridge"]["asks"], 2)


if __name__ == "__main__":
    unittest.main()
