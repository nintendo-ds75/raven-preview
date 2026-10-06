# Verification recorded on 2026-10-06

This is execution evidence for the reconstructed preview plus the workflow
changes, not a guarantee of complete model reasoning or production readiness.
The runtime checkpoint is `764b1c4095d3aceadaee4f9e332378c0bebb69a0`, built on
public reconstructed baseline `e258e47865d746c45b8dc9f8769b9732d46f1078`.
Later evidence-only documentation does not change that tested runtime.

## Actual live inference

The full [sanitized result](../evals/results/live-workflow-2026-10-06.json)
passed with actual Anthropic responses, synthetic Slack HTTP, synthetic human
inputs and authenticated Raven HTTP/MCP. No model output was replaced in this
run. The configured models were `claude-sonnet-5` and
`claude-haiku-4-5-20251001`.

- 12 physical provider requests, all HTTP 200; 14,324 input and 3,174 output
  tokens. The run capped physical attempts, including retries, at 30.
- Rough task discovery, inferred contact, natural referral, revised answer
  readback and attributed confirmation passed.
- A newly randomized cohort and threshold were preserved exactly. A live
  adaptive interview asked about the newly introduced unresolved environment,
  retained grounded caveats and remained unapproved.
- The model-backed coding host read the signed answer through MCP and generated
  a restricted Python expression. The acceptance host actually executed 16
  positive, negative and exact-boundary cases before finishing.
- Advisory review completed and identified the caller code it had not examined.
  The exported proof retained the submitted diff with integrity valid and
  authenticity unverified, as intended for this self-contained digest format.

The model-generated change is a small constrained expression, not a general
coding benchmark or a complete production invoicing integration. The staging
interview was an unapproved draft separate from the confirmed billing decision.
No actual employee approval or live Slack workspace is represented by this run.

### Failures found and fixed

The first full live run failed during adaptive interview. A focused replay
showed a grounded clarifying question with an empty answer but explanatory text
in the proposed-rationale field. That shape correctly failed the readback's
quote requirement. The prompt now explicitly distinguishes question-only
clarification from a substantive answer; one bounded validation repair resends
original human evidence and a fixed error code. Rejected model text never
becomes an approved source.

A later focused run exposed omission of the structured caveat list during
clarification. The prompt now explicitly preserves stated exceptions and
unresolved limitations even when no answer can yet be proposed. It does not
invent caveats when none were stated. Neither fix relaxed the grounding,
authority or full-workflow acceptance checks. Exact captured failure shapes and
persistent invalid-answer rejection have regression tests.

Fourteen requests were used across the initial failure and focused diagnosis;
12 more were used by the passing full run. These are recorded historical
measurements, not a cost or latency promise.

## Docker, PostgreSQL and Chromium

The disposable Docker 28.3.3 / PostgreSQL 17 test environment completed a fresh
Compose installation with empty storage, no demo seed and verified restart
persistence. After rebuilding runtime `764b1c4`, the aggregate with PostgreSQL
enabled and Teams dependencies installed **ran 1,524 tests, 17 skipped, zero
failures**, in 482.787 seconds.

All four actual Chromium suites passed on that same runtime:
`tests/browser.cjs`, `tests/browser-agents.cjs`, `tests/brief-browser.cjs` and
`tests/browser-interview.cjs`. The browser speech APIs were simulated; this does
not establish physical microphone recognition or audible speech.

The 17 skipped cases were individually rerun to capture their actual reasons;
all 17 remained skipped, with zero failures or errors. These are the recorded
reasons, grouped by test module:

| Tests | Count | Exact skip reason |
|---|---:|---|
| `test_agents_contract.SDKContractTests` | 4 | Optional OpenAI SDK is not installed |
| `test_customer_text.CustomerTextTests` | 3 | requires source documentation |
| `test_docs_links` | 4 | requires the source checkout and documentation |
| `test_fixture_hygiene.TrackedFilesTests` | 2 | requires a Git checkout |
| `test_setup_shell.SetupShellTests` | 4 | host launcher is not packaged in the application image |

Source-checkout checks below complement the application-image run; skipped
container cases are not counted as passes.

## Native and independent database checks

- Latest native aggregate on runtime `764b1c4`: **ran 984 tests, 4 skipped,
  zero failures**, in 147.256 seconds.
- Independent PostgreSQL 17.11 contract aggregate on `61948c9`: **ran 532
  tests, zero skipped, zero failures**, with actual psycopg and Teams crypto
  dependencies.
- Subsequent managed-execution delta on `e584c0d`: **ran 23 PostgreSQL tests,
  zero skipped, zero failures**, including scope clarification and explicit retry.
- Interview runtime repair on `318f2b2`: **ran 42 PostgreSQL tests, zero
  skipped, zero failures**; the following `764b1c4` change tightened only caveat
  prompt wording.
- Documentation links, fixture hygiene, customer-facing text and launcher
  checks were rerun in the source checkout: **ran 17 tests across four suites,
  zero skipped, zero failures**. Static checks also covered the new report and
  sanitized JSON, including relative links, evidence counts and secret patterns.
- Fresh stdlib-only environment completed real stdio MCP plus HTTP task,
  ingest, signoff, restart, historical reuse and correction flows.
- SQLite backup and PostgreSQL dump/restore preserved the checked decision,
  event and proof data in disposable restore targets.

Focused passes above are not relabeled as a new full PostgreSQL aggregate.
Expected negative-test error messages and socket ResourceWarnings occur in the
native log; a green result does not mean warning-free execution.

## Real open-source implementation

A separate clean Click checkout was used for an actual output-retention change,
including 20 new native test cases. The native coding agent used Raven's real
MCP/HTTP transport and explicit synthetic owner responses, then implemented the
change. Click's full default pytest suite reported **2,282 passed, 24 skipped,
31,000 deselected, one expected failure and zero unexpected failures**. These
are pytest outcome counts, unlike the aggregate unittest run totals above.
Ruff check/format and compilation also passed. The change remained a local
commit; no upstream contributor was contacted or represented as approving it.

The actual submitted patch and local Git diff were byte-identical, with SHA-256
`dca524d478f5017e2a81b7cc8724dbefc901dbf1479e6f0bc69608e3250cee71`.
Raven's proof binds the change to three attributed synthetic decisions. A
standalone nested Codex CLI could not start in that environment; the native
agent-driven transport run is not mislabeled as that CLI execution.

## Repeatable commands and boundaries

```sh
python -m unittest discover -s tests -q
BRIDGE_TEST_POSTGRES=1 DATABASE_URL="$TEST_DATABASE_URL" python -m unittest discover -s tests -q
BRIDGE_TEST_POSTGRES=1 DATABASE_URL="$TEST_DATABASE_URL" python -m unittest discover -s tests -p test_postgres.py -q
node tests/browser.cjs
node tests/browser-agents.cjs
node tests/brief-browser.cjs
node tests/browser-interview.cjs
python evals/slack_contract_harness.py --output /tmp/raven-slack-contract.json
python evals/live_workflow_acceptance.py --run-live --max-calls 30 --output /tmp/raven-live-workflow.json
```

Run the last command only after secure provider configuration and approval.
See [the Slack contract guide](slack-contract-testing.md) for the synthetic
HTTP boundary and focused live diagnostics.

Remaining external boundaries include actual Slack installation/scopes and
human identities, live Microsoft Teams registration/delivery, physical
microphone recognition and audible speech, independently verified test
attestations, and organization-specific access/retention/residency guarantees.
The app still requires its host to call Raven and resume work; it does not
silently control arbitrary agents, merge pull requests or provision production
infrastructure. See the [acceptance matrix](acceptance-2026-10-06.md).
