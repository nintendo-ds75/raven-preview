"""When no path resolves but the question names a MAINTAINERS section by
its title, that section's people are asked, and the evidence says the
area is a guess from the title. One word a title happens to share with
the question is not a match: measured on the pathless qemu replay, single
shared words (page, command, record, stub) named the wrong section."""

from fixtures import OfflineCase

from bridge.resolve import SECTION_GUESS_WEIGHT, resolve_paths
from bridge.routing import route


class SectionGuessTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = self.warm_store("qemulike", "section.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)

    def test_a_whole_section_title_stands_in_for_the_area_when_nothing_else_resolves(self):
        question = "Should network devices announce multiqueue by default?"
        hits = resolve_paths(self.graph, "qemulike", question)
        self.assertEqual([h.path for h in hits], ["hw/net/", "net/"])
        self.assertTrue(all(h.weight == SECTION_GUESS_WEIGHT for h in hits))
        self.assertEqual(hits[0].why, "no path resolved; the question names the MAINTAINERS section "
                                      "'Network devices', whose files stand in for the area")
        picked = route(self.graph, "qemulike", question)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Kwame Asante")
        self.assertIn("MAINTAINERS lists Kwame Asante for net/ (Network devices)", picked[1])
        self.assertIn("area: " + hits[0].why, picked[1])

    def test_one_shared_word_is_not_a_section(self):
        for question in ("Should devices reset faster after migration?", "Should the network stack retry?",
                         "Should machines boot quietly?"):
            self.assertEqual(resolve_paths(self.graph, "qemulike", question), [], question)
            self.assertIsNone(route(self.graph, "qemulike", question), question)

    def test_a_path_the_question_names_leaves_the_section_out(self):
        hits = resolve_paths(self.graph, "qemulike", "Should network devices use net/tap.c by default?")
        self.assertEqual(hits[0].path, "net/tap.c")
        self.assertFalse(any("stand in for the area" in h.why for h in hits), hits)

    def test_an_inherited_path_outranks_the_section_guess(self):
        hits = resolve_paths(self.graph, "qemulike", "Should network devices announce multiqueue by default?",
                             hints=["util/cutils.c"])
        self.assertEqual([h.path for h in hits], ["util/cutils.c"])

    def test_a_change_to_maintainers_itself_still_names_the_section_firmly(self):
        hits = resolve_paths(self.graph, "qemulike", "Should MAINTAINERS add me as a reviewer of network devices?")
        section = [h for h in hits if h.path == "hw/net/"]
        self.assertTrue(section and section[0].weight > SECTION_GUESS_WEIGHT, hits)
        self.assertIn("the question changes the MAINTAINERS section 'Network devices'", section[0].why)


if __name__ == "__main__":
    import unittest
    unittest.main()
