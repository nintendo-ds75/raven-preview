"""Shared fixtures: the synthetic repositories, built once per process
in one temporary directory removed at exit; a template database with
each repository ingested once, copied per test so a test that writes
to its store still has a copy of its own; and the offline environment
every test runs under (no model rungs, no live git, no API key).

The repositories are deterministic: fixed identities, dates counted in
days from a fixed epoch, a MAINTAINERS file and CODEOWNERS with the
shapes the routing replay loop (bench/routing) found on real projects."""

from __future__ import annotations

import atexit
import datetime
import functools
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The repository root, so a test module runs directly from the checkout
# (python3 tests/test_x.py) as well as under unittest discover.
ROOT = Path(__file__).resolve().parents[1]

# A shell that runs a Raven (./dev, ./setup, a pilot) exports that
# deployment's settings; every test here builds its own server and store
# and must not inherit them. Measured on a reviewer's machine: the running
# app's BRIDGE_PUBLIC_URL made an isolated test server advertise the app's
# URL, and one assertion failed for a reason that had nothing to do with
# the code. The model backend switches stay: OfflineCase turns the model
# rungs off itself, and a run may pin BRIDGE_MODEL_API=none on purpose.
DEPLOYMENT_ENV = (
    "BRIDGE_PUBLIC_URL", "BRIDGE_AUTH", "BRIDGE_AUTH_ALLOW_SIGNUP", "BRIDGE_ADMIN_TOKEN", "BRIDGE_SECRET",
    "BRIDGE_WORKSPACE_NAME", "BRIDGE_DEV_LOGIN", "BRIDGE_LOGIN_FILE", "BRIDGE_DEMO", "BRIDGE_USER_NAME", "BRIDGE_INGEST_REPO",
    "BRIDGE_INGEST_REPO_DIR", "BRIDGE_GITHUB_REPOS", "BRIDGE_GITHUB_ORG", "BRIDGE_GITHUB_SYNC_MINUTES",
    "BRIDGE_GITHUB_CLIENT_ID", "BRIDGE_GITHUB_CLIENT_SECRET", "BRIDGE_GITHUB_APP_SLUG", "BRIDGE_GITHUB_APP_FILE",
    "BRIDGE_GITHUB_APP_CLIENT_ID", "BRIDGE_CONFORMANCE_MODEL", "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_WEBHOOK_SECRET",
    "SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "SLACK_FALLBACK_CHANNEL", "SLACK_API_BASE", "TEAMS_WEBHOOK_URL",
    "OPENAI_API_KEY", "OPENAI_WEBHOOK_SECRET",
)
for _name in DEPLOYMENT_ENV:
    os.environ.pop(_name, None)


def ready_server(store, *args, **kwargs):
    """Existing feature tests run inside an already-created workspace.

    Onboarding tests deliberately use make_server directly to test the gate.
    """
    from bridge.server import make_server
    store.graph.set_setting('workspace_name', 'Test workspace')
    return make_server(store, *args, **kwargs)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge.ingest import index_repo  # noqa: E402
from bridge.store import Store  # noqa: E402


@functools.cache
def workdir() -> Path:
    """One temporary directory for the whole process, removed at exit."""
    temp = tempfile.TemporaryDirectory(prefix="bridge-tests-")
    atexit.register(temp.cleanup)
    return Path(temp.name)


# ---------------- git helpers ----------------

def run_git(repo: Path, *args: str, env: dict | None = None) -> None:
    """git in the repository, with no user or system configuration read."""
    base = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(repo)}
    if env:
        base.update(env)
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env={**os.environ, **base})


def git_env(author: tuple[str, str], day: int, committer: tuple[str, str] | None = None,
            epoch: datetime.date = datetime.date(2026, 6, 1)) -> dict[str, str]:
    """The identity and date variables for one commit: (name, email) of
    the author and of the committer (the author when not given), dated
    `day` days from the epoch, the minute taken from the day so commits
    on one day still differ."""
    an, ae = author
    cn, ce = committer or author
    d = epoch + datetime.timedelta(days=day)
    date = f"{d.isoformat()}T10:{abs(day) % 60:02d}:00 +0000"
    return {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce,
            "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}


def _seed_files(repo: Path, files: list[str]) -> None:
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"// {f}\n")


# ---------------- the qemu-like repository ----------------

PEOPLE = {
    "oriel": ("Oriel Vance", "oriel@riscv.example"),
    "tamsin": ("Tamsin Reed", "tamsin@synthco.example"),
    "kwame": ("Kwame Asante", "kwame@net.example"),
    "beatrix": ("Beatrix Hale", "beatrix@integrator.example"),
    "old": ("Old Timer", "old@past.example"),
    "sweep": ("Sweep Author", "sweep@tree.example"),
    "bot": ("dependabot[bot]", "bot@example"),
    "rich": ("Rich Reviewer", "rich@tcg.example"),
}
MAINTAINERS = """QEMU-like Maintainers
=====================

General Project Administration
------------------------------
M: Beatrix Hale <beatrix@integrator.example>
F: *
F: */

RISC-V machines
---------------
M: Oriel Vance <oriel@riscv.example>
R: Old Timer <old@past.example>
S: Supported
F: hw/riscv/
F: include/hw/riscv/

Network devices
---------------
M: Kwame Asante <kwame@net.example>
S: Maintained
F: hw/net/
F: net/
"""


def _qemu_env(author: str, committer: str | None = None, day: int = 0) -> dict[str, str]:
    return git_env(PEOPLE[author], day, PEOPLE[committer] if committer else None)


def _qemu_commit(repo: Path, rel: str, text: str, subject: str, author: str, committer: str | None = None,
                 day: int = 0, trailers: str = "") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text((p.read_text() if p.exists() else "") + text + "\n")
    run_git(repo, "add", "-A")
    msg = subject + ("\n\n" + trailers if trailers else "")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", msg, env=_qemu_env(author, committer, day))


@functools.cache
def build_qemulike() -> Path:
    """A qemu-like repository: a MAINTAINERS file, review trailers, an
    accepting committer, an integrator who merges every pull, a bot, a
    tree-wide sweep, and an inactive old-timer. Built once per process."""
    repo = workdir() / "qemulike"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "gc.auto", "0")
    (repo / "MAINTAINERS").write_text(MAINTAINERS)
    files = ["hw/riscv/virt.c", "hw/riscv/boot.c", "include/hw/riscv/virt.h", "hw/net/virtio-net.c",
             "hw/net/e1000.c", "net/tap.c", "util/bufferiszero.c", "util/cutils.c", "include/qemu/cutils.h",
             "tcg/tcg.c", "accel/tcg/translate-all.c", "docs/about.rst"]
    _seed_files(repo, files)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "Initial tree", env=_qemu_env("beatrix", day=-800))
    # Old Timer wrote hw/riscv two years ago and vanished.
    for i in range(6):
        _qemu_commit(repo, "hw/riscv/virt.c", f"// old {i}", f"hw/riscv/virt: old change {i}", "old", day=-750 + i)
    # Tamsin contributes riscv patches; Oriel reviews and commits them.
    rb = "Reviewed-by: Oriel Vance <oriel@riscv.example>"
    for i in range(10):
        _qemu_commit(repo, "hw/riscv/virt.c" if i % 2 else "hw/riscv/boot.c", f"// change {i}",
                     f"hw/riscv/virt: add feature {i}", "tamsin", committer="oriel", day=i * 5, trailers=rb)
    _qemu_commit(repo, "include/hw/riscv/virt.h", "// hdr", "hw/riscv/virt: export header", "tamsin",
                 committer="oriel", day=55, trailers=rb)
    # Kwame maintains net and commits his own and others' patches.
    for i in range(6):
        _qemu_commit(repo, "hw/net/virtio-net.c", f"// net {i}", f"hw/net/virtio-net: fix {i}", "tamsin",
                     committer="kwame", day=i * 7, trailers="Reviewed-by: Kwame Asante <kwame@net.example>")
    for i in range(3):
        _qemu_commit(repo, "net/tap.c", f"// tap {i}", f"net/tap: tweak {i}", "kwame", day=20 + i)
    # util: Rich reviews Tamsin's bufferiszero work.
    for i in range(5):
        _qemu_commit(repo, "util/bufferiszero.c", f"// z {i}", f"util/bufferiszero: speed up test {i}", "tamsin",
                     committer="rich", day=30 + i, trailers="Reviewed-by: Rich Reviewer <rich@tcg.example>")
    # A bot touches everything under hw/net and include.
    for i in range(8):
        _qemu_commit(repo, "hw/net/e1000.c", f"// bot {i}", f"chore: bump dep {i}", "bot", day=40 + i)
    # One tree-wide sweep by a stranger touches every file once.
    for f in files:
        p = repo / f
        p.write_text(p.read_text() + "// sweep\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "tree-wide: reformat every file", env=_qemu_env("sweep", day=58))
    # Beatrix merges every pull request (the integrator).
    for i in range(6):
        run_git(repo, "checkout", "-q", "-b", f"pull-{i}")
        _qemu_commit(repo, "tcg/tcg.c", f"// tcg {i}", f"tcg: optimize {i}", "rich", day=60 + i)
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", f"pull-{i}", "-m", f"Merge tag 'pull-tcg-{i}' into staging",
                env=_qemu_env("beatrix", day=61 + i))
    return repo


# ---------------- the node-like repository ----------------

NODE_PEOPLE = {
    "soren": ("Soren Berg", "soren@node.example"),
    "ines": ("Ines Moreno", "ines@node.example"),
    "casimir": ("Casimir Vale", "casimir@node.example"),
    "v8bot": ("Yara Lindgren", "yara@node.example"),
    "author": ("Some Contributor", "some@node.example"),
    "landed": ("GitHub", "noreply@github.com"),
}


@functools.cache
def build_nodelike() -> Path:
    """A node-like layout: module files named after the area (lib/sqlite.js,
    src/node_sqlite.cc, doc/api/sqlite.md), a vendored deps/ tree full of
    other projects' names, a teams-only CODEOWNERS, squash commits landed
    by GitHub with Reviewed-By trailers. Built once per process."""
    repo = workdir() / "nodelike"
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "gc.auto", "0")
    files = ["lib/sqlite.js", "lib/util.js", "lib/internal/http2/compat.js", "src/node_sqlite.cc",
             "src/node_sqlite.h", "src/node_http2.cc", "doc/api/sqlite.md", "doc/api/util.md",
             "deps/v8/src/objects/objects.cc", "deps/v8/src/sqlite/statement.cc", "deps/npm/lib/util.js",
             "test/sqlite/test-sqlite-open.js", "test/parallel/test-util.js", ".github/workflows/doc.yml",
             "tools/doc/html.mjs"]
    _seed_files(repo, files)
    (repo / ".github" / "CODEOWNERS").write_text("/lib/sqlite.js @nodejs/sqlite\n/src/node_sqlite* @nodejs/sqlite\n/deps/v8/* @nodejs/v8-update\n")
    run_git(repo, "add", "-A")

    def node_env(author, day):
        return git_env(NODE_PEOPLE[author], day, NODE_PEOPLE["landed"])

    run_git(repo, "commit", "-q", "-m", "Initial import", env=node_env("soren", -400))

    def land(rel, subject, reviewer, day, author="author"):
        rn, re_ = NODE_PEOPLE[reviewer]
        p = repo / rel
        p.write_text(p.read_text() + f"// {subject}\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", f"{subject}\n\nPR-URL: https://example/pull/{day}\nReviewed-By: {rn} <{re_}>",
                env=node_env(author, day))

    for i in range(6):
        land("src/node_sqlite.cc", f"sqlite: fix {i}", "casimir", i)
        land("lib/sqlite.js", f"sqlite: expose {i}", "casimir", 10 + i)
    for i in range(6):
        land("doc/api/util.md", f"doc: clarify util {i}", "ines", 20 + i)
        land("lib/util.js", f"util: tweak {i}", "soren", 30 + i)
    for i in range(8):
        land("deps/v8/src/objects/objects.cc", f"deps: update V8 to 13.{i}", "v8bot", 40 + i)
        land("deps/v8/src/sqlite/statement.cc", f"deps: V8 sqlite bits {i}", "v8bot", 50 + i)
    for i in range(4):
        land("lib/internal/http2/compat.js", f"http2: compat {i}", "ines", 60 + i)
    return repo


# ---------------- the synthco monorepo ----------------

SYNTHCO_PEOPLE = {
    "priya": ("Priya Sharma", "priya@synthco.example"),
    "raj": ("Raj Patel", "raj@synthco.example"),
    "sarah": ("Sarah Chen", "sarah@synthco.example"),
    "alex": ("Alex Rivera", "alex@synthco.example"),
    "bot": ("dependabot[bot]", "bot@synthco.example"),
}
SYNTHCO_EPOCH = datetime.date(2026, 3, 1)
SEEDED_PRS = [
    (18, "priya", "Define billable unit for usage metering",
     "One billable unit is one completed assistant response. Retries, failures, and cached responses do not count."),
    (31, "priya", "Exclude trial accounts from metering",
     "Trial accounts are excluded from metered usage counts entirely until conversion."),
]


def synthco_env(key: str, day: int) -> dict[str, str]:
    return git_env(SYNTHCO_PEOPLE[key], day, epoch=SYNTHCO_EPOCH)


@functools.cache
def build_synthco() -> Path:
    """A synthetic monorepo: blame concentrations, a deliberately stale
    CODEOWNERS entry, Reviewed-by trailers, merge commits and squashed
    PRs as records, a bot. Built once per process."""
    repo = workdir() / "synthco"
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "gc.auto", "0")
    files = {"billing": ["invoice.py", "meter.py", "pricing.py", "tests/test_meter.py"],
             "gateway": ["app.py", "rate_limit.py", "routes.py"]}
    for service, names in files.items():
        for f in names:
            p = repo / service / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f'"""{service}/{f}"""\n')
    (repo / "README.md").write_text("# synthco\n")
    (repo / "CODEOWNERS").write_text("billing/ @priya\ngateway/ @sarah\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "Initial synthco monorepo", env=synthco_env("alex", 0))
    plan = [("priya", "billing", i % 3) for i in range(14)] + [("alex", "billing", 1)] * 3 \
        + [("raj", "gateway", i % 3) for i in range(12)] + [("sarah", "gateway", 0)] \
        + [("bot", "gateway", 2)] * 4
    seeded = {6: SEEDED_PRS[0], 12: SEEDED_PRS[1]}
    day = 1
    for i, (eng, service, fidx) in enumerate(plan, start=1):
        rel = f"{service}/{files[service][fidx]}"
        p = repo / rel
        p.write_text(p.read_text() + f"# change {i} by {eng}\n")
        body = ""
        if i % 3 == 0:
            rname, remail = SYNTHCO_PEOPLE["alex"]
            body = f"\n\nReviewed-by: {rname} <{remail}>"
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", f"{service}: update {Path(rel).name} ({i})" + body, env=synthco_env(eng, day))
        if i in seeded:
            number, author, title, prbody = seeded[i]
            run_git(repo, "checkout", "-q", "-b", f"pr-{number}")
            p.write_text(p.read_text() + f"# pr {number}\n")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", f"{title} (work)", env=synthco_env(author, day))
            run_git(repo, "checkout", "-q", "main")
            run_git(repo, "merge", "-q", "--no-ff", f"pr-{number}", "-m", f"Merge pull request #{number}: {title}",
                    "-m", prbody, env=synthco_env(author, day))
        if i == 20:
            (repo / "billing" / "credit.py").write_text("x = 1\n")
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", "Add prepaid credit rollover (#77)",
                    "-m", "Unused prepaid credits roll over for the remaining annual term. Decided with finance after "
                          "the renewal review; add-on SKUs are excluded from rollover.", env=synthco_env("priya", day))
        day += 1
    return repo


# ---------------- template databases ----------------

BUILDERS = {"qemulike": build_qemulike, "nodelike": build_nodelike, "synthco": build_synthco}


@functools.cache
def template_db(fixture: str) -> tuple[Path, dict]:
    """The database with one fixture repository ingested, built once per
    process, and the stats index_repo reported. Tests copy the file
    rather than ingest again."""
    repo = BUILDERS[fixture]()
    path = workdir() / f"{fixture}-template.db"
    store = Store(path)
    stats = index_repo(store.graph, repo)
    # A copy of the file alone must be the whole database.
    store.graph.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    store.graph.close()
    return path, stats


class OfflineCase(unittest.TestCase):
    """A test that never reaches a model or git at ask time: the semantic
    and live rungs are off and no API key is in the environment, all of
    it restored afterwards, and a temporary directory of the test's own."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # No model, even a test that turns the semantic rung on: a claude CLI
        # on the PATH would otherwise answer it, slowly and differently each
        # run. A test that wants a reading fakes the client.
        env = patch.dict(os.environ, {"BRIDGE_SEMANTIC": "0", "BRIDGE_LIVE": "0",
                                      "BRIDGE_CLAUDE_BIN": "/nonexistent/claude-offline"})
        env.start()
        self.addCleanup(env.stop)
        from bridge.canvas import wait_for_background
        self.addCleanup(lambda: wait_for_background(10))
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def warm_store(self, fixture: str, name: str = "") -> Store:
        """A store with the fixture repository already ingested: this
        test's own copy of the template, free to write to."""
        template, _stats = template_db(fixture)
        target = Path(self.temp.name) / (name or f"{fixture}.db")
        shutil.copyfile(template, target)
        return Store(target)
