"""A question that quotes an identifier is about the code that spells it.
Measured on a pathless replay of qemu: once every path word was gone the
identifiers were the signal left, and the symbol rung either never ran
(a weaker word guess pre-empted it) or found nothing for a mnemonic no
function defines."""

import functools
from pathlib import Path
from unittest.mock import patch

from fixtures import OfflineCase, git_env, run_git, workdir

from bridge import resolve
from bridge.ingest import index_repo
from bridge.resolve import (SYMBOL_DEFINED_WEIGHT, SYMBOL_MENTION_WEIGHT, SYMBOL_SHARED_WEIGHT, SYMBOL_USED_WEIGHT,
                            code_names,
                            mentions_of, resolve_paths, symbol_hits)
from bridge.routing import route
from bridge.store import Store

CHAR = ("Char Maintainer", "char@example.test")
ARM = ("Arm Maintainer", "arm@example.test")
UTIL = ("Util Author", "util@example.test")
FILES = {
    "hw/char/imx_serial.c": "/* registers: UFCR at 0x90 */\n\nvoid imx_serial_reset(IMXSerialState *s)\n{\n    s->ufcr = 0;\n}\n",
    "include/hw/char/imx_serial.h": "void imx_serial_reset(IMXSerialState *s);\n",
    "target/arm/tcg/translate-a64.c": "/* UQADD, SQADD */\n",
    "target/arm/tcg/gengvec.c": "/* gen UQADD */\n",
    "target/arm/tcg/a64.decode": "UQADD 0101\n",
    "target/arm/tcg/sve.decode": "UQADD_zzz 0111\nUQADD 1\n",
    "tcg/aarch64/tcg-target.c.inc": "/* host UQADD */\n",
    "util/buffer.c": "void buffer_empty(Buffer *b)\n{\n}\n",
    "include/qemu/buffer.h": "void buffer_empty(Buffer *b);\n",
    "ui/vnc.c": "    if (buffer_empty(&vs->output)) {\n",
    "accel/stubs/tcg-stub.c": "void cpu_loop_exit(CPUState *cpu)\n{\n}\n",
    "accel/tcg/cpu-exec-common.c": "void cpu_loop_exit(CPUState *cpu)\n{\n    siglongjmp();\n}\n",
}


@functools.cache
def build_repo() -> Path:
    repo = workdir() / "symbols"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "gc.auto", "0")
    for rel, text in FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "Initial tree", env=git_env(UTIL, -300))
    owners = {"hw/char/imx_serial.c": CHAR, "target/arm/tcg/translate-a64.c": ARM, "util/buffer.c": UTIL,
              "ui/vnc.c": UTIL}
    for i in range(6):
        for rel, who in owners.items():
            p = repo / rel
            p.write_text(p.read_text() + f"// change {i}\n")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", f"{rel}: change {i}", env=git_env(who, i * 3))
    return repo


class SymbolRoutingTests(OfflineCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = build_repo()

    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "symbols.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        index_repo(self.graph, self.repo, repo_name="symbols")

    def hits(self, question):
        return {h.path: h for h in resolve_paths(self.graph, "symbols", question)}

    def test_a_defined_function_names_its_source_not_the_header_that_declares_it(self):
        question = "Should we fix the reset value in imx_serial_reset()?"
        hits = symbol_hits(self.graph, "symbols", question)
        self.assertEqual(list(hits), ["hw/char/imx_serial.c"])
        self.assertEqual(hits["hw/char/imx_serial.c"].weight, SYMBOL_DEFINED_WEIGHT)
        self.assertEqual(hits["hw/char/imx_serial.c"].why,
                         "the question names imx_serial_reset, defined in hw/char/imx_serial.c")
        self.assertEqual(resolve_paths(self.graph, "symbols", question)[0].path, "hw/char/imx_serial.c")
        self.assertEqual(route(self.graph, "symbols", question)[0], "Char Maintainer")

    def test_a_name_several_sources_define_names_each_less_firmly(self):
        hits = self.hits("Should we remove the cpu_loop_exit() stub?")
        self.assertEqual(sorted(hits), ["accel/stubs/tcg-stub.c", "accel/tcg/cpu-exec-common.c"])
        self.assertTrue(all(h.weight == SYMBOL_SHARED_WEIGHT for h in hits.values()))

    def test_a_mnemonic_nothing_defines_names_the_directory_that_spells_it(self):
        question = "Should we improve UQADD and SQADD?"
        hits = self.hits(question)
        self.assertIn("target/arm/tcg/", hits)
        self.assertEqual(hits["target/arm/tcg/"].weight, SYMBOL_MENTION_WEIGHT)
        self.assertEqual(hits["target/arm/tcg/"].why,
                         "the question names UQADD; 4 of the 5 files that spell it are under target/arm/tcg/")
        self.assertEqual(route(self.graph, "symbols", question)[0], "Arm Maintainer")

    def test_a_register_spelled_in_one_file_names_that_file(self):
        hits = self.hits("Should we fix the reset value of UFCR?")
        self.assertEqual(hits["hw/char/imx_serial.c"].weight, SYMBOL_MENTION_WEIGHT)
        self.assertIn("appears only in hw/char/imx_serial.c", hits["hw/char/imx_serial.c"].why)

    def test_a_name_the_question_uses_rather_than_changes_points_at_its_definer_as_a_guess(self):
        hits = symbol_hits(self.graph, "symbols", "Should we clarify the intent using buffer_empty()?")
        self.assertEqual(list(hits), ["util/buffer.c"])
        self.assertEqual(hits["util/buffer.c"].weight, SYMBOL_USED_WEIGHT)
        self.assertEqual(hits["util/buffer.c"].why,
                         "the question uses buffer_empty, defined in util/buffer.c; the file that calls it is not named")
        changed = self.hits("Should buffer_empty() also reset the offset?")
        self.assertAlmostEqual(changed["util/buffer.c"].weight, SYMBOL_DEFINED_WEIGHT)

    def test_the_symbol_outranks_a_plain_word_that_happens_to_be_a_filename(self):
        hits = resolve_paths(self.graph, "symbols", "Should we fix the buffer handling in imx_serial_reset()?")
        self.assertEqual(hits[0].path, "hw/char/imx_serial.c")

    def test_a_name_spelled_everywhere_names_nothing(self):
        with patch.object(resolve, "MENTION_MAX_FILES", 2):
            self.assertEqual(mentions_of(self.graph, "symbols", ["UQADD"]), {})
            self.assertNotIn("target/arm/tcg/", self.hits("Should we improve UQADD?"))
        self.assertEqual(mentions_of(self.graph, "symbols", ["UFCR"]), {"UFCR": ["hw/char/imx_serial.c"]})

    def test_code_names_take_mnemonics_but_not_shouting(self):
        self.assertEqual(code_names("Should we mark MOVNTI as not valid with prefixes 0x66, 0xF2?"), ["MOVNTI"])
        self.assertEqual(code_names("Should we fix the TODO list in the README and MAINTAINERS?"), [])
        self.assertEqual(code_names("Should i386 support modules_per_die in X86CPUTopoInfo?"),
                         ["modules_per_die", "X86CPUTopoInfo"])


if __name__ == "__main__":
    import unittest
    unittest.main()
