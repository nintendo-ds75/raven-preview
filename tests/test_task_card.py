"""The task card: a short title derived from the prompt when a host
registered the prompt as both title and goal, and the agent named as
people know it (Claude Code, Codex, Cursor), not by its host identifier."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fixtures import OfflineCase  # noqa: E402

from raven import attention, canvas  # noqa: E402
from raven.agent_names import display_name  # noqa: E402
from raven.authz import Actor  # noqa: E402
from raven.config import Config  # noqa: E402

PROMPT = ("We want to start charging our Enterprise customers for Pilot based on how much they use it. "
          "Right now we eat the cost. Plan the feature, then implement it.")


class TitleTests(OfflineCase):
    def test_a_title_that_is_the_whole_prompt_becomes_its_first_clause(self):
        self.assertEqual(canvas.task_title(PROMPT, PROMPT), "Start charging our Enterprise customers for Pilot")
        self.assertEqual(canvas.task_title("", PROMPT), "Start charging our Enterprise customers for Pilot")

    def test_the_agents_own_title_is_kept(self):
        self.assertEqual(canvas.task_title("Add usage pricing", PROMPT), "Add usage pricing")
        self.assertEqual(canvas.task_title("Fix the bug", ""), "Fix the bug")

    def test_a_derived_title_is_short_and_has_no_trailing_period(self):
        for goal in (PROMPT, "Fix the bug.", "Can you make the nightly invoice job idempotent?",
                     "Please add a retry to the fetcher so that transient errors do not fail the sync job entirely",
                     "x" * 100 + " and then some more words after that"):
            title = canvas.task_title(goal, goal)
            self.assertLessEqual(len(title), canvas.TITLE_WIDTH + canvas.TITLE_SLACK + 1, title)
            self.assertFalse(title.endswith("."), title)
            self.assertTrue(title, goal)
        self.assertEqual(canvas.task_title("Fix the bug.", "Fix the bug."), "Fix the bug")
        self.assertEqual(canvas.task_title("Can you make the nightly invoice job idempotent?",
                                           "Can you make the nightly invoice job idempotent?"),
                         "Make the nightly invoice job idempotent")

    def test_a_long_first_line_of_the_prompt_is_shortened_too(self):
        first = PROMPT.split(". ")[0] + "."
        self.assertEqual(canvas.task_title(first, PROMPT), "Start charging our Enterprise customers for Pilot")


class TaskCardTests(OfflineCase):
    """What raven_start_task stores, as Being built and For you read it."""

    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "task-card.db")
        self.cfg = Config()

    def start(self, **data):
        return canvas.start_task(self.store, self.cfg, {"repo": "qemulike", "agent": "claude",
                                                        "requester": "Tamsin Reed <tamsin@synthco.example>", **data})

    def test_the_prompt_registered_as_title_and_goal_is_stored_once_as_the_goal(self):
        """Measured on the Harborline dry run: the host hook sent the prompt
        as both, and the card showed it twice."""
        task = self.start(title=PROMPT, goal=PROMPT, client_key="host:1", paths="hw/riscv/virt.c")
        run = self.store.graph.get_task(task["task_id"])
        self.assertEqual(run["title"], "Start charging our Enterprise customers for Pilot")
        self.assertEqual(run["goal"], PROMPT)
        # A retry with the same key and the same prompt is the same task.
        again = self.start(title=PROMPT, goal=PROMPT, client_key="host:1", paths="hw/riscv/virt.c")
        self.assertEqual(again["task_id"], task["task_id"])
        # A missing title derives too, instead of being refused.
        task2 = self.start(goal="Switch the default interrupt controller in hw/riscv/virt.c to the aplic.",
                           paths="hw/riscv/virt.c")
        self.assertEqual(self.store.graph.get_task(task2["task_id"])["title"],
                         "Switch the default interrupt controller in hw/riscv/virt.c to the aplic")

    def test_an_agents_own_one_line_title_stays(self):
        task = self.start(title="Switch the virt default to the aplic", goal=PROMPT, paths="hw/riscv/virt.c")
        self.assertEqual(self.store.graph.get_task(task["task_id"])["title"], "Switch the virt default to the aplic")

    def test_the_agent_is_named_as_people_know_it(self):
        self.assertEqual(display_name("claude"), "Claude Code")
        self.assertEqual(display_name("codex"), "Codex")
        self.assertEqual(display_name("cursor"), "Cursor")
        self.assertEqual(display_name("Demo · Claude Code"), "Demo · Claude Code")
        self.assertEqual(display_name(""), "Coding agent")
        task = self.start(title="Switch the virt default to the aplic", paths="hw/riscv/virt.c")
        self.assertEqual(self.store.graph.get_task(task["task_id"])["agent"], "Claude Code")
        # A task an older build stored under the host identifier reads the same.
        old = self.store.add_run({"title": "Older task", "agent": "codex", "repo": "qemulike"})
        state = self.store.state()
        by_id = {r["id"]: r for r in state["runs"]}
        self.assertEqual(by_id[old["id"]]["agent"], "Codex")
        self.assertEqual(by_id[task["task_id"]]["agent"], "Claude Code")
        did = self.store.graph.add_decision(old["id"], "Which plan is metered?", "policy", "pending",
                                            repo="qemulike", owner="Oriel Vance", path="hw/riscv/virt.c")
        self.assertEqual(next(d["agent"] for d in self.store.state()["decisions"] if d["id"] == did), "Codex")
        oriel = Actor(id=self.store.add_person({"name": "Oriel Vance"})["id"], name="Oriel Vance")
        view = attention.overview(self.store, oriel)
        self.assertEqual([item["agent"] for item in view["needs_you"]], ["Codex"])
        self.assertIn("Codex keeps building", view["needs_you"][0]["meanwhile"])
