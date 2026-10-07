# Pending captured-source freshness

This report validates source commit
`2cf06687d1146f0db0d3e28f9ce2759fad3d1135` (tree
`b8e9581d16e2ad833054470b317380345d935f80`), following
`38d1d72ec52f0c89c335fe0e3a596226f7dcad17`. Exact tested file hashes and individual
outcomes are in [results.json](results.json). This is candidate validation, not a
statement of release or deployment status.

## Problem and correction

The earlier capture candidate durably queued an edit/delete but kept the old
snapshot eligible until the importer succeeded. A supporting approval could
still pass the actual completion gate during that interval, including importer
backoff or worker downtime.

A committed newer pending mutation now makes that captured source uncertain for
current use. The shared freshness predicate blocks exact source reads, current
citations, source retrieval, automatic answer reuse, sign-off and completion.
The dependent-decision traversal carries this gate through completed historical
intermediates to active consumers. This begins at the transaction that durably
accepts the mutation, without depending on a running worker.

Pending state does not rewrite immutable source snapshots, signatures or proofs.
An identical-content import removes the gate atomically without creating a
material source version or new approval. Material changes/deletion retain the
ordinary source-version and invalidation path. Older, duplicate, malformed and
unknown-message callbacks do not create a new gate or clear accepted pending
work. The original identity, queue bound and dual-original recovery behavior are
unchanged.

## Verification

The exact-source combined run passed 101 selected SQLite tests, with zero
failures, errors or skips: 39 captured-Slack cases (including eight new freshness
regressions), 60 existing source/Slack compatibility tests and two connector
freshness checks. Compilation and diff checks also passed. An independent review
found no blocking findings in this correction.

The new cases exercise queued edit and deletion, persisted backoff and restart,
primed source caches across stores, source reads and automatic reuse, actual
sign-off and finish gates, rollback of acceptance, concurrent finish after a
committed callback, completed intermediate decisions, immutable proof bytes, and
no-op restoration of the original signatures and versions. A test fixture first
froze a proof before adding dependency links, which legitimately changed its live
comparison; the fixture now freezes the complete setup before testing pending
state and recovery.

Reproduction command:

```sh
PYTHONPATH=tests:. python -m unittest \
  test_slack_capture test_context_memory test_source_observation test_record_state \
  test_slack_conversation.SlackIngressTests test_slack_conversation.DurabilityTests \
  test_portal.RecordsAndTeamsTests.test_records_from_outside_git_by_api_and_from_slack \
  test_context_connectors.ConnectorTests.test_expiry_fails_closed_even_when_worker_is_not_running \
  test_context_connectors.ConnectorTests.test_missing_filtered_result_is_unavailable_not_proven_deleted -q
```

Slack callbacks and transport were synthetic; no real provider calls or messages
were used. PostgreSQL is registered but untested in this environment. No full
aggregate suite or live Slack certification was performed. Signer-identity
validation remains incomplete; these ingestion tests do not certify broader
authorization. The gate cannot detect upstream changes whose callbacks Raven
never receives and accepts.

## Correction file manifest

- `bridge/slack_capture.py`: pending-source predicate and lookup indexes
- `bridge/context_connectors.py`: shared freshness and dependent-decision gate
- `docs/slack.md`: durable-acceptance freshness contract
- `docs/context-memory.md`: pending uncertainty versus applied observations
- `tests/test_slack_capture.py`: eight source-freshness regressions

This evidence directory contains the report and the complete test/file-hash
manifest. The [earlier capture report](../captured-slack-2026-10-07/README.md)
remains evidence for its explicitly named earlier source commit.
