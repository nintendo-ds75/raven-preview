# Evaluation audit foundation validation

Tested against Raven `8b6fed22a9b6d1a52ffdf3f5c335dae4ebffbb3d` plus the
audit tooling in this commit. This is tooling validation, not a new live coding
agent run or a full product acceptance result. No inference calls were made.

34 focused Python tests and six viewer state assertions passed. The Python run
covers audit fixtures, proxy transport, replay-driver accounting, documentation
links and tracked-file address hygiene. [Test output](tests.log).

```sh
PYTHONPATH=tests:. python3 -m unittest test_audit_replay test_audit_proxy \
  evals.real_oss_remote.test_driver test_docs_links \
  test_fixture_hygiene.TrackedFilesTests -v
node tests/audit-viewer.cjs
```

## Historical repository boundary

An online clone of `urllib3/urllib3` was frozen at
`3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, with cutoff
`2025-10-06T00:00:00Z`. The independent object store contains 4,299 commits and
22,449 objects. The present commit could not be read from it. Manifest, worktree,
brief and source-snapshot validation passed. See [captured results](frozen-urllib3.json).

The last 500 historical commits have 87 distinct author identities; the largest
share is 22.8%. These are Git identities, not verified unique humans. This checks
that the fixture is not a single-author toy; it does not establish organizational
authority. The demonstration brief was not independently blinded and is not a
scored task. The full clone and fixture remain outside the repository.

## Viewer and transport

The [offline viewer](audit.html) imports eight real MCP calls from the earlier
`live-source-loop-2026-10-06/host-b-mcp.json` run. Its
[17-event ledger](archived-example.jsonl) explicitly labels the import as
retrospective. Original response observations are retained; this does not claim
an audit chain existed at the time of the earlier run.

Computer use checked event navigation, the empty initial state, the final current
answer and source version, filtering, and browser errors. The viewer initially
let an old saved proof overwrite the current displayed decision. It now derives
state only from explicit observed node views; a regression covers old proofs,
signature scopes and advisory requirements. [Screenshot](audit-desktop.png).

The proxy test launches a real subprocess and local HTTP server. It verifies
request/response capture, credential redaction, unchanged host-visible responses,
and preservation of the request ID on a transport failure. Incremental observation
tests verify that unchanged reads are not relogged and prior states stay intact.

## Scope and remaining work

The tools validate supplied inputs and record observations. A trusted runner must
still isolate the host and Raven from future data, keep the rubric outside their
mounts, pin audit heads externally, and collect independent native test results.
The legacy replay runner now records incremental observations, but has not been
rerun with a paid host in this change and is not a sealed acceptance cohort.

Production host hooks, MCP Events, automatic resume, new connectors, per-source
permission enforcement and customer support access are planned work, not shipped
by this tooling. The existing signer-identity review remains unresolved.

See the [engineering plan](../../../docs/engineering-and-evaluation-plan.md) and
[tool guide](../../audit/README.md) for the release order and replay contract.
