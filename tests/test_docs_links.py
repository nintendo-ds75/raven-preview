"""Published documentation points at the public snapshot and real files."""
import re
import subprocess
import unittest
from fixtures import ROOT

def _tracked_text():
    if not (ROOT / ".git").exists() or not (ROOT / "docs").is_dir():
        raise unittest.SkipTest("requires the source checkout and documentation")
    paths = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files'], text=True).splitlines()
    return {rel: (ROOT / rel).read_text() for rel in paths if rel.endswith('.md') and (ROOT / rel).exists()}

class PublicLinksTests(unittest.TestCase):
    def test_customer_docs_do_not_link_to_private_implementation(self):
        for rel, text in _tracked_text().items():
            self.assertFalse(re.search(r'github\.com/nintendo-ds75/(?:bridge|bridged)(?![\w-])', text), rel)

_NUMBER_WORDS = {8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
                 15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen"}
_SCOPE_PREFIXES = ("chat", "im", "mpim", "channels", "groups", "users", "app_mentions", "assistant", "search", "files",
                   "reactions", "pins", "team")
_SCOPE_RE = re.compile(r"`((?:%s):[a-z_.]+)`" % "|".join(_SCOPE_PREFIXES))
_EVENT_RE = re.compile(r"`(message\.[a-z_]+|app_mention|member_joined_channel|team_join)`")
_RELATIVE_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")


class DocsAgreeTests(unittest.TestCase):
    """The documents describe one product: the tool list, the Slack
    scopes and events, and the files they link to agree with the code
    and with each other."""

    def test_the_reference_names_every_mcp_tool_and_counts_them(self):
        _tracked_text()
        from bridge.mcp import TOOLS
        text = (ROOT / "docs" / "reference.md").read_text()
        names = [t["name"] for t in TOOLS]
        for name in names:
            self.assertIn(f"| `{name}` |", text, name)
        word = _NUMBER_WORDS[len(names)]
        self.assertEqual(text.count(f"{word} tools"), 2, f"the reference should say '{word} tools' twice")
        for n, other in _NUMBER_WORDS.items():
            if n != len(names):
                self.assertNotIn(f"{other} tools", text)

    def test_slack_scopes_and_events_match_the_manifest_everywhere(self):
        _tracked_text()
        import json
        manifest = json.loads((ROOT / "docs" / "slack-manifest.json").read_text())
        scopes = set(manifest["oauth_config"]["scopes"]["bot"])
        events = set(manifest["settings"]["event_subscriptions"]["bot_events"])
        for rel in ("docs/slack.md", "docs/reference.md", "PILOT.md"):
            text = (ROOT / rel).read_text()
            named = set(_SCOPE_RE.findall(text))
            self.assertEqual(named, scopes, f"{rel} names the scopes {sorted(named)}; the manifest has {sorted(scopes)}")
            seen = set(_EVENT_RE.findall(text))
            self.assertEqual(seen, events, f"{rel} names the events {sorted(seen)}; the manifest has {sorted(events)}")

    def test_relative_links_in_markdown_point_at_files_in_the_repository(self):
        broken = {}
        for rel, text in _tracked_text().items():
            if not rel.endswith(".md"):
                continue
            for target in _RELATIVE_LINK.findall(text):
                if "://" in target or target.startswith(("mailto:", "#")):
                    continue
                if not (ROOT / rel).parent.joinpath(target).exists():
                    broken.setdefault(rel, []).append(target)
        self.assertEqual(broken, {}, f"links to files that are not in the repository: {broken}")


if __name__ == "__main__":
    unittest.main()
