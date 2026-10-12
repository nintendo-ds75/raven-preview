# Pytest customer loop, October 12, 2026

See [the report](../../../../reviews/main-4746708-pytest-e2e.md) for successes,
failures, repairs and limitations. This is one supervised live experiment, not a
benchmark distribution or proof of general correctness.

* `brief.txt`, `host-instructions.txt`, `oracle.json`, `test_independent.py` and
  `boundary.json` record the task, restrictions and oracle sealed before the host
  ran. No paths, owners or expected decisions were supplied in the task prompt.
* `mcp.jsonl` contains actual hook, Claude and supervisor requests and responses.
  `claude-actions.jsonl` contains the actual session's visible text and tool
  calls/results, including both resumes. Hidden reasoning is excluded.
* `slack-messages.json`, `connector-calls.json`, `fixture-state.json` and
  `services.py` show the simulated provider interfaces and payloads. Mount a
  writable copy at `/fixtures` and rename `fixture-state.json` to `state.json` to
  run that simulator. Its old header refers to the fixture's original run date.
* `mcp_proxy.py`, `launch.py` and `resume_once.py` record the tracing and host
  launch mechanics. Replace `<E2E_ROOT>` with a fresh private directory. They
  assume the isolated Docker network/container names in the script; they are
  evidence, not a turnkey deployment installer. Credentials are excluded.
* `task-trace.json`, `tree-final.json`, `proof-final.json` and `proof-check.json`
  capture the completed real-host task. `conformance-attempts.json` keeps both
  the incomplete first reading and the successful retry.
* `cache-probe.json` records a separate evaluator-driven reuse check after the
  host finished. `proof-after-restart.json` preserves the false stale result;
  `proof-after-fix.json` and `restart-check.json` show the same immutable bundle
  becoming current after the comparison fix. The problem was downstream reuse,
  not lost storage during restart.
* `audit.jsonl` is the evaluator's redacted and independently rechained action
  ledger. `audit-head.json` pins the private original and public projection
  separately. Integrity fields inside redacted proof projections identify the
  private original; they are not new signatures over the redacted documents.
* `pytest.patch` is the exact host-submitted change. File hunks match the final
  working tree, with a different order from canonical `git diff`; see
  `submitted-patch-check.json`. The source project's MIT notice is preserved
  verbatim in `LICENSE.pytest`.
* `*-before.log` preserves reproductions of the product defects. Other logs,
  `browser-observations.json`, supervisor receipts and `validation.json` record
  the independent checks, real browser observations and host outcomes.

Repository contributors are pseudonymized in traces. Synthetic contacts are
fictional. The upstream copyright notice is deliberately unchanged. Tokens,
personal task-link secrets and workstation paths are redacted. These fixtures
contain controlled test data and public code, not customer data.

To rerun acceptance checks, apply `pytest.patch` to a clean checkout of
`661682c25a9ff844a66ebd9b95d9925b8c4b65c8`, preserve its ancestral version tag
`8.5.0.dev0`, install pytest's development dependencies, and run this folder's
`test_independent.py` with that checkout's `src` on `PYTHONPATH`. Also run
`python -m pytest testing/test_junitxml.py -q` there. The independent script
launches subprocesses with its Python interpreter and inherits `PYTHONPATH`.

To repeat the live loop, start a fresh Docker/PostgreSQL Raven instance, ingest
only the sealed history, attach the local Slack/Jira/docs fixtures, and install
the normal Claude project hooks and HTTP MCP connection. Give Claude only the
brief plus the recorded harness restrictions. Keep the oracle outside both host
and server mounts. Supply human responses through signed simulated Slack events
and their actual confirmation codes. Resume using the installed supervisor,
not a scripted replacement for the coding host. Inference can incur charges;
IDs, wording, timing and outcomes will vary.
