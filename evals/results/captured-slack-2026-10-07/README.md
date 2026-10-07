# Bounded captured Slack mutation recovery

This report validates candidate source commit
`1a9cb7c8fc281617c17eff584d008f0e893a37f3` (tree
`bc309a1c989c7a80596de12b6a3c54c9063c6ff3`), built on
`80ede34d04fa7314951e1477f881f7f2d80945d4` (base tree
`5f4151878f3f2ef8460303339ff72b99228c4385`). It preserves the context connector
and host lifecycle implementation at that base. The tested file hashes in
`results.json` identify the exact implementation, documentation and test bytes.
This report is candidate validation, not a statement of release or deployment
status.

## Behavior

- Only an accepted explicit non-thread `record:` message enrolls a canonical
  workspace/channel/message identity. Generic and old legacy imports do not.
- Edits use nested `message.ts`, deletes use `deleted_ts`, and exact integer
  mutation clocks order observations without floating-point conversion.
- Import, capture commit and applied receipt share one transaction. A receipt
  cannot hide an importer failure. A crash before acknowledgement recovers its
  saved reply without importing again.
- Pending mutations belong to the message identity rather than a parent ingress
  job. Both edits and deletions survive first-job retry exhaustion when two
  originals (`message` and `app_mention`) were accepted and the second captures
  later. A pending mutation also survives until an operator retries its single
  failed original.
- One newest mutation per identity is retained, with a global 1,000-identity
  bound and a 20,000-character callback-text bound. Overflow returns HTTP 503;
  accepted work is not evicted. Import failures retry with backoff and appear in
  existing inbound failure status. Unconnected-workspace rows do not starve the
  connected workspace's queue.
- Removing the marker or deleting the message retires source evidence. A later
  marker restores a withdrawn capture, but a deletion is terminal. Material
  source changes invalidate current supporting decisions while preserving old
  version rows, signature history and completed proof bytes.
- Generic Jira/other imports remain caller-refreshed. `latest_observation` records
  local acceptance; it is not proof of upstream synchronization.

The callback identity contract was checked against Slack's official
[message_changed documentation](https://docs.slack.dev/reference/events/message/message_changed/)
and [message_deleted documentation](https://docs.slack.dev/reference/events/message/message_deleted/)
on 7 October 2026.

## Verification

The final combined run is recorded in [results.json](results.json), including all
91 executed test IDs, individual outcomes, base commit/tree, UTC time and SHA-256
hashes of the ten changed implementation/documentation/test files. It ran 31 new
capture tests and 60 existing source/Slack compatibility tests on SQLite, with
zero failures, errors or skips.

Reproduction command:

```sh
PYTHONPATH=tests:. python -m unittest \
  test_slack_capture test_context_memory test_source_observation test_record_state \
  test_slack_conversation.SlackIngressTests test_slack_conversation.DurabilityTests \
  test_portal.RecordsAndTeamsTests.test_records_from_outside_git_by_api_and_from_slack -q
```

The new coverage includes real local signed HTTP ingress, signature rejection,
retryable queue overflow, bounded/coalesced processing, exact clock identity,
out-of-order/tied/malformed clocks, atomic failure injection, receipt/reply crash
recovery, concurrent workers, restart through the actual background worker,
workspace-setting changes, explicit enrollment, old-record isolation, source
invalidation and immutable proof export. Slack transport and content are
synthetic. No Slack history, provider, inference, credential or real-message
operation was performed.

An independent code review found no concrete blockers. The initial test pass
contained two fixture/assertion mistakes (a helper assumed the default timestamp;
a history assertion expected no new audit entries after invalidation). These
were corrected and are not counted as earlier successful runs.

PostgreSQL registration is included, but PostgreSQL was not run: this environment
has no PostgreSQL client/server or container command. No full aggregate suite or
live Slack certification was performed. Signer-identity validation remains
incomplete, and these ingestion tests do not certify broader authorization.

## File manifest

- `bridge/slack_capture.py`: durable enrollment and mutation processing
- `bridge/slack_events.py`: accepted-original enrollment and mutation queue ingress
- `bridge/delivery.py`: atomic capture path, replay and failure/retry integration
- `bridge/server.py`: retryable mutation queue overflow response
- `docs/slack.md`: bounded capture behavior, recovery and migration limits
- `docs/context-memory.md`: caller refresh and local-observation semantics
- `docs/context-connectors.md`: explicit capture boundary beside transient search
- `tests/test_slack_capture.py`: new synthetic storage/transport regressions
- `tests/test_portal.py`: canonical captured-source locator assertion
- `tests/test_postgres.py`: register the new suite for the existing PG runner
- `evals/results/captured-slack-2026-10-07/README.md`: scope and verification report
- `evals/results/captured-slack-2026-10-07/results.json`: complete test/file manifest
