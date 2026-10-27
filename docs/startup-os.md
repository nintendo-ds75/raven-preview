# Persistent objectives in Raven

**Today** adds an evidence-to-work head around the existing Raven task, judgment,
source, and execution core. It does not replace that core or infer permission
from a transcript.

## The loop

1. Import permission-scoped evidence (including [meeting transcripts](meeting-adapters.md))
   or use the existing GitHub, Slack, Jira, documentation and context connectors. For Vexa, explicitly authorize per-occurrence notice/transcription and workspace disclosure, then enable background sync. After a separate authorized Join, read-only polling imports the exact occurrence transcript; no recurring-room consent or automatic attendance is inferred.
2. **Find opportunities** runs a bounded read-only search/read investigation through the configured Raven model
   backend before proposing any judgment. It searches the whole eligible corpus, not only recent meeting chunks, preserves exact quotes and reports unread sources/partial meeting coverage. Retrievable factual answers return as progress rather than needless judgment requests. It can return no useful move. Exact quoted record/version citations
   are checked; inaccessible, changed or invented evidence rejects the result.
   No model backend means unavailable, not a fabricated recommendation.
3. **Review & take point** prepares an ordinary Raven judgment node. The proposed
   outcome, limits and positive/negative/boundary cases are inside that node's
   signed scope. The existing owner, required signers and source-review rules
   apply. The initial click is not approval.
   The signer never reads that scope as JSON: the inbox card, the decision
   dialog, the personal task page and the Slack message show the same short
   card instead (the outcome, the question, what the move will and won't do and
   its edge cases, the bound, the cited evidence, and a link back to Today),
   built from the stored scope without adding to it. The JSON stays the text
   the signature binds to.
4. Only the exact current signed answer and scope activate the move. A signed
   refusal or corrected scope does not accidentally authorize the old proposal.
   A durable worker dispatches the signed move to the selected configured
   execution harness for **private repository preparation only**. Missing host
   configuration or unresolved earlier work stays visibly blocked.
5. Coding work uses Raven's existing private-copy tools, judgment gates, durable
   session resume and isolated command runner. Objective pause/stop propagates
   to linked coding tasks. Before each host tool, current objective/core scope
   is checked. No push, deployment, external communication, meeting join or
   purchase is authorized by an objective.
6. A completed coding run does **not** achieve the business outcome. Keep the
   thread, record a checkpoint/next check time, and observe the actual result.
   Marking achieved is explicitly a human attestation, not an independent test.

An explicit manual **Take point on an outcome** saves a person's outcome and
bounds; its explicit preparation checkbox starts the selected private coding worker. Uncheck it to save without dispatch. This path needs no
fictional source citation. Source-originated proposals always need provenance
and the ordinary judgment review.

## Persistence and scheduling

Additive SQLite/PostgreSQL tables retain objectives, source/version associations,
run bindings, lifecycle events, request receipts, proposals and evaluation
leases. Read-only findings can wake an existing waiting coding session with exact evidence and unchanged authority. The server's objective worker ticks every `BRIDGE_OBJECTIVE_TICK_SECONDS` (15 by default) while it has work: a workbench, a model backend, or saved objectives, proposals or watches. Otherwise it only re-checks that cheaply every 60 seconds, so a default install runs no objective work at all. Each tick handles due
checkpoints and, when a model backend exists, reevaluates an active objective only when its repository's evidence corpus changed since that objective was last evaluated or a checkpoint came due; the corpus revision is computed once per repository per tick. The meeting observer idles the same way until an observer is enabled or Vexa is configured (`BRIDGE_MEETING_TICK_SECONDS`). Coding-host readiness probes (`node`, `codex`, `opencode` versions) run only for a host whose credential and model are set, at most once per `BRIDGE_HOST_PROBE_SECONDS` (60 by default); page polls read the cached answer, and `GET /api/state` probes nothing while the workbench is off. An administrator can also opt a repository into an evidence watch before any objective exists; current membership/role and watch revision are rechecked around inference. Disabling a watch fences in-flight publication. Saved local-operator identities do not acquire privilege in authenticated deployments. At most one material open proposal per
objective is retained. Evaluation identities include evidence, scope revision,
harness and model/prompt configuration; lease-owner fencing prevents an expired
worker from publishing over its replacement. Inference occurs outside writer
transactions; publication revalidates input and the lease in one transaction. Automatic background investigation requires an API backend with an enforceable output-token bound; the existing internal `claude-cli` backend remains available only for explicit manual investigation. This does not disable the Claude SDK coding harness. Failed evaluations have exponential cooldown and stop after three attempts for identical inputs; material source/model changes produce a fresh evaluation. An authenticated scan may explicitly send `retry_failed:true` with a new `request_key` to authorize a fresh bounded attempt set after setup repair; automatic workers never set this override. A blocked retry is not a completed investigation or a no-op.

Proposals retain rejection rationale. Source access revocation withholds copied
objective/proposal content and blocks dependent workers. It does not silently
erase historical evidence or make old signatures valid for new facts. A changed
signed proposal is reconciled explicitly, not interpreted as yes/no by a model. Metadata-only catalog observations retain separate disclosure dependencies: revocation blocks their reuse and changed displayed metadata invalidates their scope, but an unread source body-only update does not revoke signed work. Corpus changes can still trigger fresh investigation; actually cited body evidence remains exact-version bound.

The database remains one shared-workspace trust boundary, **not unrelated-tenant
isolation**. Imported meeting evidence must be shareable with every workspace
reader. Native provider or workspace ACLs are not invented by this feature.

## Execution and verification

See [execution harnesses](execution-harnesses.md) for the exact supported runtime
and model-provider distinctions, readiness blockers and offline/live coverage.
See [artifact verification](artifact-verification.md) for independent operator
profiles, candidate-byte bindings and the limitations of repository-owned tests.
The new verification API is `GET /api/workbench/{id}/verification` (workspace
member or above; receipts bind candidate bytes and operator profiles) and an
administrator can `POST` the same route with an operator-configured `profile_id`.
A client cannot submit an arbitrary command as an independent profile. A
listing reuses the last candidate tree hash while the task's base revision,
patch digest and decision vector are unchanged; a receipt-producing run always
hashes the actual bytes.

## API boundaries

- `GET /api/objectives`, `GET /api/objectives/{id}`: visible objective state.
- `POST /api/objectives`: explicit human request; idempotent `request_key`.
- `POST /api/objectives/scan`: bounded investigation of a named repository. Manual
  scans spend their own per-repository budget of the same size as the automatic
  one (6 model requests per rolling hour); over budget the result is
  `status: throttled` with `next_retry_at` and no model request. An
  administrator may pass `override: true` on a manual scan to spend outside it.
- `POST /api/objectives/watch`: administrator opt-in/disable for new-evidence investigation.
- `POST /api/meetings/{id}/background-sync`: explicit administrator opt-in/disable for periodic read-only Vexa observation; separate from Join, Stop and Revoke.
- `POST /api/proposals/{id}/accept|dismiss`: revision-checked review initiation or feedback.
  When no authority map covers the governance node, it is assigned to the accepting
  member through the inbox's own assignment with the basis `objective-accepter`
  recorded on the node; a mapped owner keeps it. A node a standing rule already
  answers is reconciled with the rule named, never activated: dismiss it and
  propose a narrower move for explicit review.
- `POST /api/objectives/{id}/pause|resume|cancel|checkpoint|dispatch|achieve`:
  `expected_revision` and `request_key` required; actor comes from authentication.
- Meeting creation, consent, import, sync, join, stop, revoke and remote deletion
  require administrator identity; joins also require explicit per-occurrence
  participant-notice/capture permissions. Capture is never auto-started by the head.

## Verification coverage

Tests use real git, SQLite, core judgments and HTTP paths with **explicit fake
inference/harness/provider transports**. Actual installed optional harness SDK
smokes are separately labeled. The full Python and Node suites, PostgreSQL
contracts and Chromium suites are required release checks. Live meeting capture,
authenticated hosted inference and deployment are separate acceptance steps;
local fixture success does not establish them. No broad connector-availability
or universal business-outcome guarantee is made.
