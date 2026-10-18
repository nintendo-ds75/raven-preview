"""Three failure classes from the qemu routing replay (bench/routing):
a release manager's merges counted as approval of every file a pull
carried, pieces of one identifier naming an unrelated file, and the verb
of a "Should <verb> ..." question taken for an area."""


from fixtures import OfflineCase

from bridge.resolve import Tree, area_word_hits, term_hits
from bridge.routing import route
from bridge.signals import MERGE_BASE_WEIGHT, _scope_signals

NOW = "2026-06-20T00:00:00+00:00"


class GatekeeperMergeTests(OfflineCase):
    """Measured on qemu: the release manager merges half of the repository's
    pulls into nearly every area, so in a file with seven changes three
    merges were 93% of its approval, over the maintainer who committed the
    patches; under docs/ ninety-eight merges outranked the committer because
    the release manager also wrote a few pages there."""

    def setUp(self):
        super().setUp()
        self.store = self.fresh_store("gatekeeper.db")
        self.graph = self.store.graph
        self.addCleanup(self.graph.close)
        self.graph.set_source("app", "git_now", NOW)
        for area in ("alpha", "beta", "gamma"):
            self.graph.upsert_artifact("app", f"{area}/code.py")

    def change(self, sha, area, people):
        self.graph.add_change("app", sha, NOW, [f"{area}/code.py"], [(name, "", role) for name, role in people])

    def seed(self):
        # The release manager merges every area's pulls; the beta
        # maintainer merges beta's own.
        for area in ("alpha", "gamma"):
            for i in range(6):
                self.change(f"merge-{area}-{i}", area, [("Release Manager", "merger")])
        for i in range(6):
            self.change(f"merge-beta-{i}", "beta", [("Beta Maintainer", "merger")])
        # alpha's own history: the area maintainer commits a contributor's
        # patches; the release manager wrote one patch there once.
        for i in range(8):
            self.change(f"alpha-{i}", "alpha", [("Contributor", "author"), ("Area Maintainer", "committer")])
        self.change("alpha-rm", "alpha", [("Release Manager", "author")])

    def test_a_merger_of_every_area_gets_little_credit_from_a_small_scopes_merges(self):
        self.seed()
        alpha = _scope_signals(self.graph, "app", "alpha/", 0)
        self.assertLess(alpha["merge_weights"]["release manager"], 0.7)
        self.assertGreater(alpha["merge_weights"]["release manager"], MERGE_BASE_WEIGHT - 1e-9)
        picked = route(self.graph, "app", "Should alpha retry on timeout?", path="alpha/code.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Area Maintainer")

    def test_a_merger_of_one_area_keeps_full_weight_there(self):
        self.seed()
        beta = _scope_signals(self.graph, "app", "beta/", 0)
        self.assertAlmostEqual(beta["merge_weights"]["beta maintainer"], 1.0)
        picked = route(self.graph, "app", "Should beta retry on timeout?", path="beta/code.py")
        self.assertIsNotNone(picked)
        self.assertEqual(picked[0], "Beta Maintainer")

    def test_substantial_independent_work_restores_a_gatekeepers_merges(self):
        self.seed()
        for i in range(4):
            self.change(f"gamma-rm-{i}", "gamma", [("Release Manager", "author")])
        gamma = _scope_signals(self.graph, "app", "gamma/", 0)
        self.assertAlmostEqual(gamma["merge_weights"]["release manager"], 1.0)


class IdentifierPieceTests(OfflineCase):
    """Measured on qemu: memory_region_init_rom_nomigrate() resolved to
    scripts/coccinelle/memory-region-housekeeping.cocci through the pieces
    "memory" and "region", ahead of the file that defines it."""

    def setUp(self):
        super().setUp()
        self.tree = Tree(["scripts/coccinelle/memory-region-housekeeping.cocci", "system/memory.c",
                          "include/system/memory.h", "hw/display/cg3.c", "hw/display/tcx.c",
                          "hw/net/virtio-net.c", "include/hw/virtio/virtio-net.h", "net/tap.c", "util/cutils.c",
                          "hw/xen/xen-mapcache.c", "include/exec/ram_addr.h", "rust/hw/char/pl011/src/device_class.rs"]
                         + [f"other/file{i}.c" for i in range(40)])

    def test_pieces_of_one_identifier_do_not_name_a_file_with_pieces_of_its_own(self):
        hits = term_hits(self.tree, "Should we stop using memory_region_init_rom_nomigrate()?")
        self.assertNotIn("scripts/coccinelle/memory-region-housekeeping.cocci", hits)
        hits = term_hits(self.tree, "Should disas prefer the cached CpuClass over the CPU_GET_CLASS() macro?")
        self.assertNotIn("rust/hw/char/pl011/src/device_class.rs", hits)

    def test_pieces_of_one_identifier_name_the_file_they_make_up(self):
        hits = term_hits(self.tree, "Should mapcache break out xen_ram_addr_from_mapcache_single()?")
        self.assertIn("hw/xen/xen-mapcache.c", hits)

    def test_the_identifier_itself_still_names_its_file(self):
        hits = term_hits(self.tree, "Should virtio-net add queues before loading them?")
        self.assertIn("hw/net/virtio-net.c", hits)
        self.assertGreaterEqual(hits["hw/net/virtio-net.c"].weight, 0.8)

    def test_two_separate_words_still_name_a_file(self):
        hits = term_hits(self.tree, "Should the memory region housekeeping script cover ROM init?")
        self.assertIn("scripts/coccinelle/memory-region-housekeeping.cocci", hits)


class VerbAreaTests(OfflineCase):
    """Measured on qemu: "Should extract x86_isa_bios_init() ..." made
    "extract" the area, through host/include/*/load-extract-al16-al8.h.inc."""

    def setUp(self):
        super().setUp()
        self.tree = Tree(["host/include/aarch64/host/load-extract-al16-al8.h.inc",
                          "host/include/generic/host/load-extract-al16-al8.h.inc",
                          "scripts/batch_rename.py", "hw/xen/xen-mapcache.c", "hw/i386/x86.c",
                          "lib/sqlite.js", "src/node_sqlite.cc", "doc/api/sqlite.md", "lib/util.js"])

    def test_the_word_before_a_code_name_is_the_action_not_the_area(self):
        self.assertEqual(area_word_hits(self.tree, "Should extract x86_isa_bios_init() from x86_bios_rom_init()?"), {})
        self.assertEqual(area_word_hits(self.tree, "Should rename InvoiceService to BillingInvoice?"), {})

    def test_a_label_that_is_a_piece_of_a_file_name_is_still_an_area(self):
        hits = area_word_hits(self.tree, "Should mapcache: Break out xen_ram_addr_from_mapcache_single?")
        self.assertIn("hw/xen/xen-mapcache.c", hits)

    def test_a_word_that_is_a_file_stem_still_brings_its_piece_matches(self):
        hits = area_word_hits(self.tree, "Should sqlite fix a use-after-free in StatementSync?")
        self.assertIn("lib/sqlite.js", hits)
        self.assertIn("src/node_sqlite.cc", hits)
