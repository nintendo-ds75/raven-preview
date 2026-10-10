"""A greeting or a purely informational question is a pass, not an
unplaced task that sends the agent to find files; a change or a decision
named without an area is still unplaced."""
from fixtures import OfflineCase

from bridge import canvas
from bridge.config import Config


class NoChangeTriageTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "triage.db")
        self.cfg = Config()

    def start(self, goal, key):
        return canvas.start_task(self.store, self.cfg, {"title": goal[:80], "goal": goal, "repo": "qemulike",
                                                        "agent": "test", "client_key": key})

    def test_greetings_and_informational_questions_pass(self):
        for i, goal in enumerate(["hi", "thanks!", "what does this repo do?", "Explain how the build is organised"]):
            started = self.start(goal, f"chat-{i}")
            self.assertEqual(started["verdict"], "pass", goal)

    def test_a_change_or_decision_without_an_area_is_still_unplaced(self):
        for i, goal in enumerate(["Agent tokens currently never expire. Add an expiry to them.",
                                  "What should the default token expiry be?"]):
            started = self.start(goal, f"change-{i}")
            self.assertEqual(started["verdict"], "unplaced", goal)
