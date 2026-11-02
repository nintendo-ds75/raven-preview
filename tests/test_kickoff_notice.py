"""A headless agent without host hooks has to be told, where it reads, to
register the task before any work. Measured on the Harborline dry run (demo
v6, run 1): with .mcp.json only, Claude Code read the repository, searched
Raven's decisions and records, implemented "the parts that don't depend on
open business decisions" in sixteen turns and never called
bridge_start_task; nothing reached Raven's log."""
from fixtures import OfflineCase
from bridge import mcp
from bridge.auth import Identity


class KickoffNoticeTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("kickoff-notice.db")
        self.addCleanup(self.store.graph.close)
        self.store.add_owner({"name": "Alex Moreno", "team": "Billing", "patterns": "app/billing/*"})
        self.alex = Identity(id="p-alex", name="Alex Moreno", role="member", kind="agent", token_label="Claude Code")

    def test_the_instructions_and_the_reading_tools_put_the_kickoff_first(self):
        self.assertTrue(mcp.INSTRUCTIONS.startswith("First call on any task: bridge_start_task"), mcp.INSTRUCTIONS[:120])
        tools = {t["name"]: t for t in mcp.TOOLS}
        for name in mcp.READ_BEFORE_KICKOFF:
            self.assertIn("registers no task", tools[name]["description"], name)
            self.assertIn("bridge_start_task", tools[name]["description"], name)
        self.assertNotIn("registers no task", tools["bridge_start_task"]["description"])

    def test_a_read_with_no_task_registered_carries_the_notice_until_the_kickoff(self):
        found = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"}, principal=self.alex)
        self.assertIn("bridge_start_task", found.get("kickoff", ""), found)
        self.assertIn("registered", found["kickoff"])
        people = mcp.call_tool(self.store, "bridge_find_people", {"topic": "billing", "repo": "harborline/harborline-app"}, principal=self.alex)
        self.assertIn("bridge_start_task", people.get("kickoff", ""), people)
        # The owner listing is paged within a byte bound, and a record
        # lookup answers the same over MCP and REST: their descriptions
        # carry the message, their answers do not.
        owners = mcp.call_tool(self.store, "bridge_list_owners", {}, principal=self.alex)
        self.assertNotIn("kickoff", owners)
        started = mcp.call_tool(self.store, "bridge_start_task", {
            "title": "Charge Enterprise for Pilot", "repo": "harborline/harborline-app",
            "goal": "Charge Enterprise customers for Pilot by use.", "client_key": "k1"}, principal=self.alex)
        self.assertTrue(started["task_id"])
        again = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"}, principal=self.alex)
        self.assertNotIn("kickoff", again, again)
        # Another person's task is not this agent's.
        other = Identity(id="p-dana", name="Dana Whitfield", role="member", kind="agent", token_label="Codex")
        theirs = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"}, principal=other)
        self.assertIn("kickoff", theirs)

    def test_a_finished_task_does_not_count_as_registered(self):
        started = mcp.call_tool(self.store, "bridge_start_task", {
            "title": "Charge Enterprise for Pilot", "repo": "harborline/harborline-app",
            "goal": "Charge Enterprise customers for Pilot by use.", "client_key": "k2"}, principal=self.alex)
        mcp.call_tool(self.store, "bridge_finish_task", {"task_id": started["task_id"], "status": "abandoned",
                                                         "reason": "wrong repository"}, principal=self.alex)
        found = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"}, principal=self.alex)
        self.assertIn("kickoff", found)

    def test_the_local_operator_on_stdio_is_told_too(self):
        found = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"})
        self.assertIn("bridge_start_task", found.get("kickoff", ""), found)
        mcp.call_tool(self.store, "bridge_start_task", {"title": "Charge Enterprise for Pilot",
                                                        "repo": "harborline/harborline-app", "client_key": "k3"})
        again = mcp.call_tool(self.store, "bridge_search_decisions", {"query": "Pilot billing"})
        self.assertNotIn("kickoff", again, again)
