"""Customer-visible text calls the product Raven. The `bridge` package,
the `bridge_*` tools and the `BRIDGE_*` settings keep their names on
purpose; so do the HTTP headers and the registered GitHub App's name."""

from __future__ import annotations

import io
import json
import re
import sys
import tokenize
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import ROOT  # noqa: E402

_WORD = re.compile(r"\bBridge\b")
# A pattern that also matches the old name in what people and agents write is not customer text.
_ALLOWED = re.compile(r"X-Bridge-|Bridge Repository Access|\(\?:Bridge\|Raven\)")
_PROSE = ("README.md", "DOCKER.md", "PILOT.md", "SCALING.md", "setup", "dev", "setup.ps1", ".env.example")


def _offenders(text: str) -> list[str]:
    return [text[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")
            for m in _WORD.finditer(text) if not _ALLOWED.search(text[max(0, m.start() - 4):m.end() + 20])]


@unittest.skipUnless((ROOT / "docs").is_dir(), "requires source documentation")
class CustomerTextTests(unittest.TestCase):
    def test_python_string_literals_say_raven(self):
        found: dict[str, list[str]] = {}
        for path in sorted((ROOT / "bridge").glob("*.py")) + [ROOT / "bridge_mcp.py"]:
            for t in tokenize.generate_tokens(io.StringIO(path.read_text()).readline):
                if t.type == tokenize.STRING:
                    hits = _offenders(t.string)
                    if hits:
                        found.setdefault(str(path.relative_to(ROOT)), []).extend(hits)
        self.assertEqual(found, {}, f"strings that still say Bridge: {found}")

    def test_web_pages_docs_and_entry_points_say_raven(self):
        found = {}
        files = list((ROOT / "web").glob("*")) + list((ROOT / "docs").glob("*.md")) + [ROOT / n for n in _PROSE]
        for path in files:
            if not path.is_file():
                continue
            hits = _offenders(path.read_text(errors="replace"))
            if hits:
                found[str(path.relative_to(ROOT))] = hits
        self.assertEqual(found, {}, f"text that still says Bridge: {found}")

    def test_the_slack_app_and_the_agent_rule_are_named_raven(self):
        manifest = json.loads((ROOT / "docs" / "slack-manifest.json").read_text())
        self.assertEqual(manifest["display_information"]["name"], "Raven")
        self.assertEqual(manifest["features"]["bot_user"]["display_name"], "Raven")
        self.assertIn("## Raven", (ROOT / "web" / "app.js").read_text())
        self.assertIn("## Raven", (ROOT / "docs" / "reference.md").read_text())


if __name__ == "__main__":
    unittest.main()
