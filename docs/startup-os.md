# Persistent objectives in Raven

**Today** adds an evidence-to-work head around the existing Raven task, judgment,
source, and execution core. It does not replace that core or infer permission
from a transcript. For what Raven is as a whole, and what it is not, read
[What Raven is](positioning.md).

## The loop

1. Import permission-scoped evidence (including [meeting transcripts](meeting-adapters.md))
   or use the existing GitHub, Slack, Jira, documentation and context connectors. For Vexa, explicitly authorize per-occurrence notice/transcription and workspace disclosure, then enable background sync. After a separate authorized Join, read-only polling imports the exact occurrence transcript; no recurring-room consent or automatic attendance is inferred.
2. **Find opportunities** runs a bounded read-only search/read investigation through the configured Raven model
   backend before proposing any judgment. It searches the whole eligible corpus, not only recent meeting chunks, preserves exact quotes and reports unread sources/partial meeting coverage. Retrievable factual answers return as progress rather than needless judgment requests. It can return no useful move. Exact quoted record/version citations
   are checked; inaccessible, changed or invented evidence rejects the result.
   No model backend means unavailable, not a fabricated recommendation: the
   button is off, and every page says so in one line under its heading
   (`Suggestions and briefs are off: no model key. Routing, records, reminders
   and signed decisions still run.`; with the internal `claude-cli` backend,
   `Manual investigation only; automatic suggestions need an API key.`; with a
   key, `Model: <model>. Suggestions on for watched repositories.`). Today
   then lists the **open loops** a person can still close without a model:
   follow-ups agents suggested that nobody adopted, answers a correction put in
   doubt, checkpoints that came due, and evidence recorded since the last
   investigation of each repository.
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
7. Today groups all of this by what it waits on, from the status the server
   computes for each objective: **Needs your decision** (open proposals and
   blocked objectives), **In progress** (a running worker, or a scope out for
   signature, named `Awaiting signed review · <owner>`), **Ready to review**
   (waiting objectives whose worker finished in the last seven days), and a
   folded **Closed** list. The sidebar's Today count adds up the open
   proposals and blocked objectives `GET /api/state` reports (`today`), next
   to `model`, which says what the configured backend allows. Nothing on
   Today posts to Slack; the inbox and its deliveries stay the only channel.

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
  When no authority map covers the governance node, the `objective_scope_fallback`
  setting says where it goes: `accepter` (the default) assigns it to the accepting
  member through the inbox's own assignment with the basis `objective-accepter`
  recorded on the node; `coordinator` hands it to the workspace coordinator when
  one is set; `none` leaves it unassigned with a routing reason that an
  administrator must place it. A mapped owner keeps it in every mode. A node a
  standing rule already answers is reconciled with the rule named, never
  activated: dismiss it and propose a narrower move for explicit review.
- `POST /api/objectives/{id}/pause|resume|cancel|checkpoint|dispatch|achieve`:
  `expected_revision` and `request_key` required; actor comes from authentication.
- Meeting creation, consent, import, sync, join, stop, revoke and remote deletion
  require administrator identity; joins also require explicit per-occurrence
  participant-notice/capture permissions. Capture is never auto-started by the head.

## The first two weeks

What a three-person team sees, day by day, using only what this page
describes. Nothing below needs a live provider until day 8.

**Day 0.** `./setup`, with the Anthropic key left blank, prints `No model key: Raven routes,
records, reminds and keeps signed decisions; it will not write briefs or
propose work. Add a key later in Connections & setup.` Today, the inbox and
Connections & setup each carry one line under the heading: `Suggestions and
briefs are off: no model key. Routing, records, reminders and signed decisions
still run.` and, until a Slack bot is connected, `No Slack connected:
questions wait in the inbox.`

**Days 1 to 3.** Each person pastes what they already have into **Add
evidence on Today**: the notes from a customer call, the pricing policy, the
paragraph of the contract that matters. Every paste becomes a versioned
source record; Today's open loops show it under *New evidence since the last
investigation*, counted per repository. Coding agents connected over MCP ask
their questions as they work; each question is routed to the person whose
area it touches and waits in the inbox, where the sidebar count says how many.
Follow-ups an agent suggested and nobody adopted appear under *Follow-ups
nobody adopted*; an answer a later correction put in doubt appears under
*Answers that need a second look*.

**Days 4 to 7.** The second time an agent asks what the first one asked, the
signed answer is reused and the inbox shows where it came from; a person
signs again only when a source changed. Someone takes point on an outcome by
hand (**Take point on an outcome**, with preparation unchecked) and saves a
checkpoint with a next check date; when it comes due, the objective moves to
**Needs your decision** and the open loops list it under *Checkpoints due*.
Nothing has been proposed, briefed or started by a model: the `Find
opportunities` button is off, with the same sentence as its reason.

**Day 8.** An administrator adds an Anthropic or OpenAI key in Connections &
setup and restarts. The line under each heading now reads `Model: <model>.
Suggestions on for watched repositories.` and the open loops block leaves
Today. The administrator enables an evidence watch on the repository
(**Find opportunities → Enable a repository watch**).

**Days 9 to 14.** The watch reads the evidence pasted in week one and
proposes at most one bounded move per repository, with the exact quotes it
rests on; it appears under **Needs your decision**. A person clicks **Review &
take point**, which puts an ordinary judgment node in the inbox, routed by the
authority map or by `objective_scope_fallback`; Today shows it under **In
progress** as `Awaiting signed review · <owner>`. The owner signs the scope
card in the inbox. The objective worker activates the move and, when a
configured execution harness is ready, dispatches a private-copy worker; when
none is, the objective says so in its `blocked_reason` and stays under **Needs
your decision** rather than pretending to run. A finished run moves the
objective to **Ready to review** for seven days; a person records a checkpoint
or attests the outcome, and the objective folds into **Closed**.

## Verification coverage

Tests use real git, SQLite, core judgments and HTTP paths with **explicit fake
inference/harness/provider transports**. Actual installed optional harness SDK
smokes are separately labeled. The full Python and Node suites, PostgreSQL
contracts and Chromium suites are required release checks. Live meeting capture,
authenticated hosted inference and deployment are separate acceptance steps;
local fixture success does not establish them. No broad connector-availability
or universal business-outcome guarantee is made.
