"""A unique directory named the way people say an area ("the Emscripten
backend") is as firm an area as a path. Measured on urllib3: the question
named the backend by its common name, the directory hit stayed at the
weight of a lone word, and nobody was routed to."""

from pathlib import Path

from fixtures import OfflineCase

from bridge.resolve import DIR_NAMED_WEIGHT, DIR_WORD_WEIGHT, Tree, dir_name_hits, resolve_paths
from bridge.routing import route
from bridge.store import Store

NOW = "2026-06-20T00:00:00+00:00"
PATHS = ["src/urllib3/contrib/emscripten/fetch.py", "src/urllib3/contrib/emscripten/connection.py",
         "src/urllib3/util/retry.py", "src/urllib3/response.py", ".github/workflows/ci.yml", "linux-user/main.c",
         "bsd-user/main.c", "hw/xen/xen.c", "python/qemu/machine.py", "docs/index.rst"]
EMSCRIPTEN = "src/urllib3/contrib/emscripten/"


class AreaNameTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.tree = Tree(PATHS)

    def hits(self, question):
        return {h.path: h for h in dir_name_hits(self.tree, question).values()}

    def test_a_unique_directory_named_as_an_area_is_as_firm_as_a_path(self):
        for question in ("Can we remove the web worker code from the Emscripten backend once JSPI is available?",
                         "Should CI run the emscripten tests in a separate Node.js job?",
                         "What minimum Node.js version should the Emscripten backend require?"):
            hits = self.hits(question)
            self.assertEqual(list(hits), [EMSCRIPTEN], question)
            self.assertEqual(hits[EMSCRIPTEN].weight, DIR_NAMED_WEIGHT)
            self.assertEqual(hits[EMSCRIPTEN].why, f"the question names the emscripten area, which is {EMSCRIPTEN}")

    def test_a_lowercase_directory_word_on_its_own_stays_a_guess(self):
        hits = self.hits("Should emscripten keep a polyfill for older runtimes?")
        self.assertEqual(hits[EMSCRIPTEN].weight, DIR_WORD_WEIGHT)
        self.assertEqual(hits[EMSCRIPTEN].why, f"the question names the directory {EMSCRIPTEN}")

    def test_two_words_that_join_into_a_directory_name_count_as_it(self):
        hits = self.hits("Should the linux user loader pass the pid along?")
        self.assertEqual(hits["linux-user/"].weight, DIR_WORD_WEIGHT)
        self.assertEqual(self.hits("Should the linux user emulation pass the pid along?")["linux-user/"].weight,
                         DIR_NAMED_WEIGHT)

    def test_three_letters_name_an_area_only_beside_an_area_noun(self):
        self.assertEqual(self.hits("Should Xen support be disabled by default?")["hw/xen/"].weight, DIR_NAMED_WEIGHT)
        self.assertEqual(self.hits("Should xen be disabled by default?"), {})

    def test_a_language_named_with_a_capital_is_the_language(self):
        self.assertEqual(self.hits("Should we rewrite the helper in Python?")["python/"].weight, DIR_WORD_WEIGHT)
        self.assertEqual(self.hits("Should the Python package drop 3.8?")["python/"].weight, DIR_NAMED_WEIGHT)

    def test_the_named_area_routes_to_the_person_who_holds_it(self):
        store = Store(Path(self.temp.name) / "area.db")
        graph = store.graph
        self.addCleanup(graph.close)
        graph.set_source("app", "git_now", NOW)
        for path in PATHS:
            graph.upsert_artifact("app", path)
        for i in range(6):
            graph.add_change("app", f"em-{i}", NOW, ["src/urllib3/contrib/emscripten/fetch.py"],
                             [("Backend Author", "", "author"), ("Backend Reviewer", "", "reviewed-by")],
                             subject=f"Emscripten fetch {i}")
            graph.add_change("app", f"core-{i}", NOW, ["src/urllib3/response.py"],
                             [("Core Author", "", "author"), ("Core Reviewer", "", "reviewed-by")],
                             subject=f"Response {i}")
        question = "Can we remove the web worker code from the Emscripten backend once JSPI is available?"
        self.assertEqual(resolve_paths(graph, "app", question)[0].path, EMSCRIPTEN)
        picked = route(graph, "app", question)
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Backend Reviewer")
        self.assertIn(f"area: the question names the emscripten area, which is {EMSCRIPTEN}", picked[1])


if __name__ == "__main__":
    import unittest
    unittest.main()
