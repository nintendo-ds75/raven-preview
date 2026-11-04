"""Who a call goes to when the authority map names several people.

Measured on the Harborline dry run (demo v6, run 6): the requester, who
decided app/billing/* and the billing category, received seven of the
eighteen calls on his own task; a "billing" word in a question about
services/usage/* sent it to the billing decider past the person who
decides those files; and the kickoff verdict named the admin, whose infra
category the word "cost" had matched, as the first contact instead of the
owners the map names for the task's area."""
from fixtures import OfflineCase
from raven import canvas
from raven.config import Config
from raven.routing import route_ranked

REPO = "harborline/harborline-app"
GOAL = ("We want to start charging our Enterprise customers for Pilot based on how much they use it. "
        "Right now we eat the cost. Plan the feature, then implement it.")


class HarborlineCase(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("precedence.db")
        g = self.graph = self.store.graph
        self.addCleanup(g.close)
        with g.transaction():
            people = {name: g.add_person(name, email=f"{name.split()[0].lower()}@harborline.example")
                      for name in ("Alex Moreno", "Jonas Lindqvist", "Raj Patel", "Dana Whitfield",
                                   "Mei Tanaka", "Tom Becker")}
            for who, kind, scope in (
                    ("Alex Moreno", "path", "app/billing/*"), ("Alex Moreno", "path", "jobs/*"),
                    ("Alex Moreno", "category", "billing"), ("Jonas Lindqvist", "path", "app/billing/*"),
                    ("Raj Patel", "path", "services/usage/*"), ("Raj Patel", "category", "data"),
                    ("Dana Whitfield", "category", "pricing"), ("Dana Whitfield", "path", "app/billing/plans.py"),
                    ("Mei Tanaka", "path", "app/pilot/*"), ("Tom Becker", "category", "infra"),
                    ("Tom Becker", "path", "infra/*")):
                g.add_authority(kind, scope, "decides", person_id=people[who], repo=REPO, source="config")
            for path in ("app/pilot/assistant.py", "app/pilot/usage_report.py", "app/billing/plans.py",
                         "app/billing/invoices.py", "jobs/nightly_invoice.py", "services/usage/events.py",
                         "infra/deploy.yaml"):
                g.upsert_artifact(REPO, path)

    def ranked(self, question, path, requester="", category=""):
        return route_ranked(self.graph, REPO, question, path=path, requester=requester, category=category)


class PathOverWordsTests(HarborlineCase):
    def test_path_authority_over_the_named_files_outranks_a_category_the_words_name(self):
        """'billable' speaks of billing; Raj decides the file the decision
        names and holds a judgment category of his own (data), so the word
        does not take the call from him. The billing decider stays a
        candidate. (A path-only owner still yields to the topic's decider:
        tests/test_delivery.py, the pricing question in Wes's billing file.)"""
        ranked = self.ranked("What counts as one billable use of Pilot?", "services/usage/events.py")
        self.assertEqual(ranked[0][0], "Raj Patel", ranked[:2])
        self.assertIn("Alex Moreno", [r[0] for r in ranked])
        self.assertTrue(any("decides the files it names" in ln and "Alex Moreno" in ln for ln in ranked[0][1]),
                        ranked[0][1])

    def test_a_category_the_agent_names_still_outranks_the_file(self):
        """Measured live on 5e967e4: a security call on a runtime file is
        the security decider's. The agent's own category keeps that."""
        ranked = self.ranked("What counts as one billable use of Pilot?", "services/usage/events.py",
                             category="billing")
        self.assertEqual(ranked[0][0], "Alex Moreno", ranked[:2])

    def test_a_word_alone_does_not_hand_the_requesters_files_to_another_area(self):
        """Dana decides plans.py; 'charges start' speaks of billing."""
        ranked = self.ranked("How many Pilot uses does Enterprise get free each month before charges start?",
                             "app/billing/plans.py")
        self.assertEqual(ranked[0][0], "Dana Whitfield", ranked[:2])


class RequesterTests(HarborlineCase):
    def test_a_co_owner_is_asked_instead_of_the_requester(self):
        """Alex and Jonas both decide app/billing/*; Alex asked."""
        ranked = self.ranked("How does the invoice show Pilot usage?", "app/billing/invoices.py",
                             requester="Alex Moreno")
        self.assertEqual(ranked[0][0], "Jonas Lindqvist", ranked[:2])
        self.assertTrue(any("does not route a question back to its requester" in ln for ln in ranked[0][1]),
                        ranked[0][1])
        # Alex's authority is not forgotten: still a candidate, after Jonas.
        self.assertIn("Alex Moreno", [r[0] for r in ranked[1:]])

    def test_the_requesters_own_call_is_labelled_when_nobody_else_holds_the_area(self):
        """Only Alex decides jobs/*. The call stays with Alex, and says so."""
        ranked = self.ranked("Should the nightly invoice job post Pilot charges in the same batch as seats?",
                             "jobs/nightly_invoice.py", requester="alex@harborline.example")
        self.assertEqual(ranked[0][0], "Alex Moreno", ranked[:2])
        self.assertTrue(any(ln.startswith("own call:") for ln in ranked[0][1]), ranked[0][1])

    def test_the_requester_is_not_asked_when_a_word_made_them_the_topical_decider(self):
        """Measured on run 6: 'billing' in the question made the requester
        the topical decider of a file Mei decides."""
        ranked = self.ranked("Is a Pilot batch prompt that fails and is retried by the worker billed as one use?",
                             "app/pilot/assistant.py", requester="Alex Moreno")
        self.assertEqual(ranked[0][0], "Mei Tanaka", ranked[:2])


class KickoffContactTests(HarborlineCase):
    def start(self, goal=GOAL, **extra):
        return canvas.start_task(self.store, Config(model_api="none"), {
            "title": goal.split(".")[0], "goal": goal, "repo": REPO, "agent": "test",
            "requester": "Alex Moreno", **extra})

    def test_the_kickoff_names_the_areas_owner_not_the_admin_the_word_cost_matched(self):
        task = self.start()
        self.assertEqual(task["verdict"], "engage", task["why"])
        people = [p["name"] for p in task["discovery"]["people"]]
        self.assertEqual(people[0], "Mei Tanaka", task["discovery"]["people"])
        self.assertNotIn("Tom Becker", task["why"])
        self.assertIn("Mei Tanaka", task["why"])
        self.assertIn("app/pilot/*", task["why"])

    def test_the_kickoff_says_plainly_when_the_task_is_the_requesters_own_call(self):
        task = self.start(goal="Change the nightly invoice job in jobs/nightly_invoice.py to post Pilot charges "
                               "in their own batch, so the Stripe sync never times out on them.",
                          paths="jobs/nightly_invoice.py")
        self.assertEqual(task["verdict"], "engage", task["why"])
        self.assertEqual(task["discovery"]["people"][0]["name"], "Alex Moreno", task["discovery"]["people"])
        self.assertIn("Alex Moreno's own call", task["why"])
        self.assertNotIn("first contact", task["why"])
