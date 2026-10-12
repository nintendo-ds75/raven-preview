# Investigation, retrieval and host wake events

Implemented against `f339c31c332d5272e92c35497cb60f2ba2766e3d` on October 12, 2026.

Raven can review what a coding host discovered, challenge missing judgment, search
sources and prior answers with learned embeddings, compare first-contact evidence,
and notify a compatible host when the task changes. The user stays in their
existing coding agent. Human authorization remains a separate enforced check.

## What changed

* `bridge_review_discovery` records the host's observations, paths, current source
  versions, decision revisions, model findings and the host's dispositions. It
  distinguishes suggested human judgments from things to investigate in code.
  No suggestion sends a message or creates approval by itself.
* Newly engaged tasks with inference enabled request another discovery review at
  finish. Changed diffs, decisions or retrieved sources require another review.
  The host links real decision nodes or records a reason for each finding.
  This reviews supplied context; it cannot establish complete requirement coverage.
* Reviews run in the background. Duplicate submissions share the saved result.
  Pending reviews block this finish check. Failed or interrupted reviews say they
  are unavailable and can be retried by ID. A dead worker becomes visible after
  ten minutes. Tree, wait, finish and workspace export expose the review; export
  includes the inputs. Unavailable review is an explicit limitation, not a claim
  of coverage, and does not substitute for any required human authorization.
* FastEmbed with BGE small English vectors nominates sources and human memories
  from the existing graph. Keyword search remains in the merge. Source links lead
  to related human answers. Eligibility is checked again after retrieval; vectors
  do not override scope, current-source checks or signoff. Docker bundles the model.
* First-contact reasoning compares supplied people and evidence, with one optional
  additional search. Candidate IDs and cited evidence are validated and audited.
  Explicit assignments and verified deciders remain authoritative. Selecting a
  contact grants no permanent authority.
* Natural Slack referrals and rule requests use the existing model conversation
  and confirmation flow. Explicit commands retain their deterministic handlers.
* The authenticated HTTP MCP endpoint implements MCP Events discovery,
  subscription, verification and unsubscription. Signed callbacks contain task
  and decision IDs and the event kind. The host reads current state through MCP.
  Local adapters continue using hooks and the existing resume supervisor.
* Subscriptions and queued deliveries are durable, scoped to an authenticated
  credential, expiring and revocable. Callback destinations must be public HTTPS;
  connections pin the validated address and verify the hostname. Retries retain
  the event ID. An actual-visible baseline plus delivery deduplication avoids
  losing a late PostgreSQL commit whose event ID is below one already delivered.
  Callback secrets are excluded from task traces and workspace JSON export.

## Validation

| Check | Result |
| --- | --- |
| Full native Python suite during implementation | 2,349 tests, 117 skips, passed |
| Affected discovery, events, proof, canvas, host-wake, Slack and docs suites after recovery/export changes | 257 passed |
| Final discovery and events suite after the event-ordering regression | 22 passed on SQLite; 22 passed on PostgreSQL 17 |
| Production image discovery and proof suites | 37 passed before the final event-ordering change |
| Final rebuilt production image discovery and MCP Events suites | 22 passed, external networking disabled |
| Seven CI DOM suites | Passed; these are DOM/API doubles, not Chromium |
| Ruff and whitespace check | Passed |
| Bundled embedding model in Docker with networking disabled | Ran as UID 10001; returned 384-dimensional vectors |
| Live component probe using frozen urllib3 and real inference | Final run 8/8 checks, 99.53 seconds |

The full suite was followed by targeted checks as changes landed; the counts are
separate runs, not one aggregate claim about a final full-suite run.

## What the live probe established

The probe ingested 250 commits up to urllib3 revision
`3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, with identities pseudonymized before
inference. Two synthetic policy records and reachable synthetic contacts provided
controlled expected answers. Inference was real; no public maintainer was contacted.

The first run found a routing defect: retrieved record text was discarded when
its author was already a candidate from history. The fix enriches that candidate.
The second run selected the intended contact but rejected an invalid discovery
response. Its exact invalid field was not retained in that earlier revision.
The prompt now states the exact ID/path contract; invalid output is retained
separately, and the same case subsequently completed.

The final run found the relevant runtime policy despite different wording,
selected its author, and interpreted a referral, a non-answer and a scoped answer.
It created no decisions or authority automatically. All three runs are preserved
in [the evidence folder](../evals/newdev/results/intelligence-2026-10-12/README.md).

## Limits still worth measuring

The discovery model labelled an internal return-value question as judgment. The
host should investigate that before asking a person. The disposition step is
there to make that review explicit; eight component checks do not demonstrate
perfect questions or production routing accuracy.

Cold embedding of a large corpus still costs time. The index is an in-process
pilot implementation with vectors in the existing database, not a measured
enterprise-scale vector service.

No new autonomous coding-host loop, real Slack installation or live OpenAI cloud
wake was run for this change. Event tests exercise authenticated MCP HTTP and a
callback receiver. OpenAI currently documents this wake interface for Work cloud
chats and dots, not arbitrary local CLI sessions. A customer connection must be
verified there before claiming it wakes their host.

Sources: [OpenAI MCP Events](https://developers.openai.com/plugins/build/mcp-events),
[Qdrant FastEmbed](https://qdrant.tech/documentation/fastembed/fastembed-quickstart/).
Setup and operational details are in [the integration guide](../docs/investigation-and-events.md).
