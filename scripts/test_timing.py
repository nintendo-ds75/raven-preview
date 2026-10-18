"""Time the Python test suite file by file and test by test.

Runs every ``tests/test_*.py`` through ``unittest`` discovery, one file per
subprocess so a file that hangs or crashes cannot hide the others, and prints
the wall time of each file, the slowest individual tests and the total.

    python3 scripts/test_timing.py                 # all files
    python3 scripts/test_timing.py test_auth test_brief.py
    python3 scripts/test_timing.py --json out.json  # machine-readable copy
    python3 scripts/test_timing.py --slowest 25     # more rows in the test table

Standard library only, like the suite itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Executed inside each subprocess. It records per-test wall time with a
# TestResult subclass and writes JSON to the path in RAVEN_TIMING_OUT.
_CHILD = r"""
import json, os, sys, time, unittest

class Timing(unittest.TextTestResult):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.durations = []
        self._t0 = {}
    def startTest(self, test):
        self._t0[test.id()] = time.perf_counter()
        super().startTest(test)
    def stopTest(self, test):
        super().stopTest(test)
        self.durations.append((test.id(), time.perf_counter() - self._t0.pop(test.id(), time.perf_counter())))

pattern = sys.argv[1]
suite = unittest.defaultTestLoader.discover("tests", pattern=pattern)
runner = unittest.TextTestRunner(stream=sys.stderr, verbosity=0, resultclass=Timing)
t0 = time.perf_counter()
result = runner.run(suite)
wall = time.perf_counter() - t0
with open(os.environ["RAVEN_TIMING_OUT"], "w") as fh:
    json.dump({
        "wall": wall,
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "durations": result.durations,
    }, fh)
sys.exit(0 if result.wasSuccessful() else 1)
"""


def _select(names: list[str]) -> list[Path]:
    files = sorted(TESTS.glob("test_*.py"))
    if not names:
        return files
    wanted = {n if n.endswith(".py") else n + ".py" for n in names}
    chosen = [f for f in files if f.name in wanted]
    missing = wanted - {f.name for f in chosen}
    if missing:
        sys.exit(f"no such test file(s): {', '.join(sorted(missing))}")
    return chosen


def _time_file(path: Path, python: str, scratch: Path) -> dict:
    out = scratch / (path.stem + ".json")
    env = dict(os.environ, RAVEN_TIMING_OUT=str(out))
    t0 = time.perf_counter()
    proc = subprocess.run(
        [python, "-c", _CHILD, path.name],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    wall = time.perf_counter() - t0
    record = {"file": path.name, "process_wall": wall, "ok": proc.returncode == 0}
    if out.exists():
        record.update(json.loads(out.read_text()))
        out.unlink()
    else:  # the child died before writing: keep the tail of its stderr
        record.update(wall=wall, tests=0, failures=0, errors=1, skipped=0, durations=[])
        record["stderr_tail"] = proc.stderr[-2000:]
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help="test file names (default: all)")
    parser.add_argument("--slowest", type=int, default=10, help="rows in the slowest-tests table")
    parser.add_argument("--json", dest="json_out", help="also write the raw records here")
    parser.add_argument("--python", default=sys.executable, help="interpreter for the child processes")
    args = parser.parse_args(argv)

    files = _select(args.files)
    scratch = Path(os.environ.get("TMPDIR", "/tmp")) / f"raven-test-timing-{os.getpid()}"
    scratch.mkdir(parents=True, exist_ok=True)
    records = []
    started = time.perf_counter()
    for i, path in enumerate(files, 1):
        rec = _time_file(path, args.python, scratch)
        records.append(rec)
        flag = "" if rec["ok"] else "  FAILED"
        skipped = f" ({rec['skipped']} skipped)" if rec.get("skipped") else ""
        print(f"[{i:3d}/{len(files)}] {rec['wall']:7.2f}s  {rec['tests']:4d} tests  {path.name}{skipped}{flag}", flush=True)
    total = time.perf_counter() - started
    try:
        scratch.rmdir()
    except OSError:
        pass

    print()
    print("Slowest files")
    for rec in sorted(records, key=lambda r: -r["wall"])[:15]:
        print(f"  {rec['wall']:7.2f}s  {rec['tests']:4d} tests  {rec['file']}")

    tests = [(f["file"], tid, d) for f in records for tid, d in f.get("durations", [])]
    print()
    print(f"Slowest {args.slowest} tests")
    for fname, tid, d in sorted(tests, key=lambda t: -t[2])[: args.slowest]:
        print(f"  {d:7.2f}s  {tid}")

    n_tests = sum(r["tests"] for r in records)
    bad = [r["file"] for r in records if not r["ok"]]
    print()
    print(f"Total: {n_tests} tests in {len(files)} files, {total:.1f}s wall "
          f"(sum of in-process file times {sum(r['wall'] for r in records):.1f}s)")
    if bad:
        print(f"Failing files ({len(bad)}): {', '.join(bad)}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(records, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
