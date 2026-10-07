# Versioned context memory

Raven stores source evidence and its decision links in the existing SQLite or
PostgreSQL database. No separate graph service is required. A source's text,
author, assignee, status, and claims of approval remain evidence only. A human
must still approve the actual question and scope through Raven's existing
identity and authority checks.

## Unsigned agent proposals

`bridge_settle_node` and `POST /api/tasks/:id/settle` accept optional
`source_evidence`: at most 64 objects containing exactly `record_id`,
`source_version_id`, and `role` (`support`, `contradiction`, `context`, or
`work_item`). Copy the opaque IDs from an actual `bridge_get_record` reading;
refs, URLs, citations in rationale, and claims of approval cannot create pins.
For example:

```json
{
  "task_id": "the-task-id",
  "node_id": "the-node-id",
  "answer": "Apply the retention policy to this scoped archive.",
  "rationale": "The current source describes this archive's retention period.",
  "source_evidence": [
    {"record_id": "the-record-id", "source_version_id": "the-observed-version-id", "role": "support"}
  ]
}
```

The answer and pins publish in one writer transaction. Every supplied pin must
identify the exact current, available source in the node's repository and
selected source namespace, with a fresh retrieval lease where applicable. A
stale second pin rejects the entire proposal. A concurrent later source change
invalidates the published reliance normally. This records the agent's stated
sources; it does not attest that the host read them or that its reasoning follows
from them.

Pins are additive. Omitting a prior pin, supplying an empty array, or adding a
`context` pin cannot remove or downgrade existing support, contradiction, or
source-decision dependencies. Existing stale dependencies must receive a
person's complete current-source review first; an agent cannot refresh them or
claim an independent human replacement. Unknown legacy provenance stays unknown.
Without `source_evidence`, a new unsupported manual proposal remains possible,
and prose alone establishes no external provenance. Existing review flags and
dependencies are retained even in that mode.

The proposal stays `source=agent`, `kind=agent`, unsigned, with sign-off required.
It clears prior partial signatures and answer attribution and supersedes a pending background model
reading. Neither exact pins nor settling authorizes a task or creates a standing
grant: use `bridge_wait` for the required human decision. The existing human
notification contains the scoped answer and task link; the source-review
readback displays every exact pin, complete source snapshot, and decision scope
before a generation-specific confirmation. The human web review also shows the
complete source bodies, metadata, and exact record/version/role pins for fresh
proposals, even when no source revalidation is pending. This display does not
change ordinary sign-off or make context-only links blocking. A changed or
oversized chat source review is refused rather than shortened into an approvable
answer. A new pin set on the same answer creates a fresh notification review epoch.

## Import and source identity

`bridge_import_record` and `POST /api/records` share one implementation. Legacy
`(repo, kind, ref)` keys remain unchanged, including distinct `ticket`, `jira`,
and `issue` kinds. For explicitly scoped imports, supply `provider` (`github`,
`jira`, `slack`, or `generic`), `namespace` (the stable installation/site or
workspace identity), and `external_id` (the object's stable ID; defaults to
`ref`). These produce an opaque stable `record_id`. Human-readable refs and
URLs are display/locator metadata, not identity or authorization.

The source-identity relation also records explicit canonical identities learned
by the existing native ingest operation for an established intent (for example,
a local Git PR followed by the native GitHub sync of that same PR key). This
preserves the stable record/key and earlier identity rather than rewriting old
snapshots. A conflicting identity already bound to another record is rejected;
text, title, author, or URL similarity is never used to merge records. Each
version and citation identifies the provider/namespace of that observation.

Two installations with the same Jira key are separate records. Two repository
owners with the same repository basename are separate scopes. An explicit
source namespace never silently adopts an old unscoped record. Ambiguous
cross-installation keys are withheld from retrieval unless the task has an
explicit source anchor selecting the relevant namespace.

The response includes `source.record_id`, `source.source_version_id`, the local
sequence, permalink, observation time, source timestamps, provider/namespace,
and whether the import materially changed the record. `bridge_get_record` and
`GET /api/records?repo=...&record_id=...` read exact immutable observed versions.
Unavailable sources refuse these content reads. Access-scope metadata is
preserved, but this is not a new external ACL synchronization service: the
existing Raven workspace access policy still applies to prior decisions/proofs.

Optional `task_id` explicitly selects a durable `work_item` anchor. `anchor_role`
may instead be `context`. A host's `client_key` is only task-resume identity;
it is never interpreted as an external work-item ID. Anchors are returned on
the task tree and trace and do not independently block or authorize the task.

GitHub and local Git ingestion continue to supply records natively. Jira/Slack
records imported by a host are generic durable imports and remain caller-refreshed,
not native Jira sync or full Slack backfill. The bot's explicit `record:` captures
have a separate, bounded [edit/delete callback path](slack.md); generic and legacy
imports do not enroll in it. Slack Real-time Search remains transient; imports
labeled `slack_realtime_search`, `realtime_search`, or `transient` are rejected.
This does not add OAuth, credentials, third-party access, or new fetches.

## Immutable observations and materiality

Every material observation has an immutable `source_versions` row. Its snapshot
contains the imported title/body, author, source-created time, status/resolution,
complete paths, locator, availability, access metadata, and available source
update/version/sequence fields. Local `observed_at` is separate. An unknown
source timestamp stays empty, never replaced by ingestion time. Bounded input
fields reject oversize values instead of silently truncating imported content.
Native connectors retain their existing documented bounded extraction limits.

Title, text, author, creation time, status, resolution, paths, URL, availability,
and access metadata are material. A fingerprint-identical reimport keeps the
same version and causes no sign-off churn. A change away and back produces a
new sequence and version even if its fingerprint repeats. Source update/version
metadata alone does not make a material revision; ordered no-op observations
still advance the ordering watermark. Older supplied source sequences or update
timestamps are rejected atomically; conflicting content for an already observed
sequence is rejected. Opaque source-version tokens are recorded, not sorted.
Without source ordering data, local revision order means observation order only.

`latest_observation` means the latest observation accepted locally by Raven. It
is not proof of an upstream synchronization, source freshness or complete event
delivery. Generic Jira, Slack and other caller imports change only when the
caller submits another snapshot. The optional Airweave adapter's separate
freshness lease and upstream sync limits still apply. For enrolled Slack captures,
a durably accepted newer callback creates immediate current-source uncertainty
until its import commits. This blocks supporting reuse, source reads, sign-off
and finish even while the worker is stopped. The receipt still names the last
applied observation; queue acknowledgement is not source application. No-op
recovery preserves immutable versions and existing signatures.

A separate `latest_observation` receipt on import, exact-record read and canonical
lookup reports the most recent accepted local import time plus the latest known supplied source
ordering/version metadata. Identical or metadata-only syncs refresh that receipt
without creating a source revision, changing a pinned citation, revoking approval
or queuing another review. It describes locally accepted data, not proof that a
provider was fetched. Older upgraded records may have an empty receipt until
the next import; migration does not invent a recent fetch. Rejected or rolled-back
imports do not advance it. Immutable version `observed_at` remains unchanged.

Imports are complete text/state observations with optional path snapshots:
provided paths replace, `[]` or empty string clears, omission retains. Omitted
author, source-created time, and URL retain the previously known values on that
identity. Explicit replacement values appear in the new version; earlier values
remain in history. Imported state uses the [record state contract](record-import-contract.md).
A Done/Closed source still cannot approve anything.

## Used sources, sign-off, and replacement

A decision has typed, exact version-pinned edges: `support`, `contradiction`,
`context`, or `work_item`. Supporting premises and contradictions are active
reliance; context/association alone is not a blocking premise. The single-record
ladder carries the selected source object into publication. Joint composition
must return a machine-readable `USED_SOURCES` set; only those exact supplied
objects are linked. Rejected neighbors are not dependencies. Old cached joint
outputs without this contract are rejected rather than assigned invented links.

All selected source heads are compared under the publication write transaction,
after composition and before the answer/edges commit. A source update during
composition rejects publication. The background model pass checks the pins again
when moving its temporary result onto the actual decision. There are no provider
calls inside these write transactions.

A material premise update atomically records the new source version and flags
exactly its active decision chains for revalidation. Live predictions/signatures
are withdrawn from authorization, while immutable decision snapshots retain the
exact earlier signed answer and source versions. Dependent rules cannot authorize
new work while their source is in review. Restoring old source content does not
restore old approval. FTS and memoized retrieval observe the committed head.

Signing existing evidence retains its dependencies. A human-authored answer also
retains them by default, including when the answer wording changes. Neither text
similarity nor matching source authors establishes independence. To deliberately
replace the former derivation, a human uses `evidence_mode: independent` through
the answer/correction API (also available as a clearly labeled checkbox in the
web answer form). Previous active derivations are retired, not erased from
history. An explicit `source_evidence` list can retain selected current supporting
pins on that replacement. Ordinary revalidation supplies the exact current pins
in `source_evidence`, a new answer, and the fresh `expected_updated_at`; stale
readbacks and stale source pins fail. Agents cannot invoke human approval tools.

Structured sources and URLs are returned on node, decision, task, and proof
responses, and displayed beside decisions. A legacy free-text citation is
`unknown` provenance. It is never resolved against today's content to fabricate
an old evidence edge.

## Migration and completed proof

Schema creation is additive and idempotent on SQLite and PostgreSQL. Legacy
source rows receive an immutable `legacy-observation` snapshot at migration
observation time; this does not assert what content existed when an older answer
was signed. Historical signature facts and independent human authority are preserved.
Known source-derived legacy rules with unknown revisions cannot automatically
authorize fresh work. Migration marks their already-active automatic consumers
and dependent chains review-needed, preserving task blockers and all completed
rows. No old decision receives a fabricated source edge. New tables preserve foreign-key
ordering for the existing SQLite-to-PostgreSQL importer.

Source revision creation, head/path refresh, invalidation, and audit events share
one write transaction. SQLite uses its existing immediate write lock; PostgreSQL
uses Raven's existing transaction advisory lock. Duplicate imports serialize;
rollback exposes none of a half-applied update. Sign-off and finish validate
current reliance under that same boundary. Proof creation/export are also read
and snapshotted under a write boundary, without model calls.

A completed proof payload/digest never changes. Export returns the saved bundle
alongside `current_sources` and current applicability/staleness. A background-only
source change is visible there without automatically making the signed decision
or proof inapplicable. A changed supporting premise marks live applicability
stale, while the old proof remains a valid immutable account of the earlier
signed snapshot. Ending a rule alone retains the existing completed-history
behavior.

## Validation

`tests/test_record_state.py` covers the separately frozen state/path correction.
`tests/test_context_memory.py` covers revisions, no-ops/reverts, exact metadata,
second-source changes, context-only changes, independent replacement, current-pin
reapproval, publication/sign-off races, rollback, duplicate concurrent import,
namespace/repository isolation, migration/restart, immutable proof versus live
staleness, explicit task anchors, and transient Slack rejection. Both are
registered with the real PostgreSQL test loader. Provider calls are not required.

### Legacy cache and standing authority

Older free-text source citations stay unknown even after a new import has the
same human-readable ref. Node/decision APIs expose `source_provenance` and an
explicit `source_notice`; a new sign-off endorses the exact current answer and
scope, not a claim that the old external citation was verified. A new reuse of
such an answer can pin the known human decision version while still leaving its
external provenance unknown. Existing independent human decisions and human-only
standing-rule authority are not blanket-revoked by migration. A known
source-derived rule with unknown source revisions instead has
`source_reuse_requires_review: true` and a visible `source_notice`, even with
`auto_rules=1`. It remains evidence for a new owner's question-specific sign-off;
that sign-off alone does not clear the external provenance uncertainty or enable
automatic reuse. The gate follows exact active source/parent/derived/depends
relationships, including completed precedents; it never guesses identities from
citation text, matching answers, authors, or refs. Typed legacy resolution events
and immutable source snapshots preserve known derivation when a human correction
had changed the row's `source` to `human`. Missing, malformed, or partial typed
provenance cannot confer automatic authority.

An explicit independent replacement can retire the old derivation. Alternatively,
the owner can select and submit current supporting `source_evidence`, all exact
`source_decision_pins`, and a fresh `expected_updated_at`. These establish a new
reviewed present derivation; they do not reconstruct what the old answer used.
Existing known premises and namespace constraints still apply. Context-only pins,
ordinary yes/sign-off, rule renewal, readbacks, and matching-ref reimports never
clear the gate. Old co-signatures do not carry into that first explicit review,
even when a partial current pin happened to have been attached earlier.

The immutable saved proof and historical signature facts remain unchanged. Proof
export reports this separate present-use limitation in `current_reuse`, with
`requires_source_review` and a reason; this is distinct from historical bundle
integrity and the saved decision's signature. Migration never changes completed
rows merely because their external revision history is unknown. A changed known
supporting source is different: A→B→A creates version 3 and cannot silently
restore signatures invalidated at B. The human must review current pins again.

Selected namespaces constrain both source-record retrieval and decision memory
that still actively relies on those namespaces, including transitive derivations.
A signature does not erase that source context. Explicit independent human policy
remains eligible under its own question/path/fact scope. Inherited parent premises
keep the canvas's existing invalidation behavior; an explicitly independent
replacement can retire that inherited reliance while preserving tree position.
Typed external `context`/`work_item` edges still do not become blocking premises.
Legacy v1 proofs without the additive empty `sources` field compare as unchanged;
the original payload is never rewritten to add it.

## Deliberate current-source review

`get_decision.source_revalidation` is a read-only proposal. It includes complete
source snapshots and exact `pins`, source-decision text and exact `decision_pins`,
`available`, a notice, and an authenticated `review_path`. It never refreshes
active edges by being read. Missing/inaccessible sources, out-of-repository or
selected-namespace mismatches, and source decisions still awaiting revalidation
make it unavailable. No source text is silently truncated for this review.

Human-source pins include `source_version_id` and `source_snapshot_sha256`,
besides `decision_id`, `updated_at`, and `historical`. The readback includes the
complete `reviewed_snapshot` (decision fields, source pins, and derivation pins),
including rationale, facts, paths, applicability, signer constraints, and rule
conditions. Pure-human chains are reviewable even when `sources` is empty.
Read-only previews prefer an existing matching immutable decision version; an
unsnapshotted current observation has an empty version ID and a content hash.
Only explicit confirmation freezes that exact observation under the writer lock.
It never reconstructs a past version from a legacy citation.

Answer/signoff compares the complete displayed human pin set and all current
external premises on its existing writer connection, then rebinds active human
derivations to those exact versions. Omitted, incomplete, stale, or changed-scope
pins fail atomically. A new same-text human revision requires fresh review.
Earlier decision snapshots and signed proof payloads keep their original pins.
Repeated review of unchanged human versions preserves new co-signatures; changed
versions require fresh co-signatures. A concurrently held second source-review
readback still requires reopening after the first signature changes the root
`updated_at`; this conservative freshness check does not erase that first
signature when the second signer reviews the unchanged source pins again.
Historical completed precedents retain
signed history and use the existing explicit current-context conversion.
An unanswered, unaccepted inbox prediction with no typed reliance is only a
hint; giving its first human answer does not adopt that hint as a premise.
Typed support, contradiction, derived, and dependency edges are never discarded
by this compatibility case. Fresh inbox hints record the source revision they
actually observed on their creation transaction; existing missing pins are
never backfilled from today's source. `source_revalidation.has_reliance` is
structural metadata: a blocked, foreign, or missing relied-on chain still sends
chat owners to the authenticated review page even when no dependency content
can safely be displayed. Human-source revalidation errors identify the affected
decision. Tests use explicit reviewed pins and the displayed confirmation code;
a bare yes or omitted pins does not satisfy this contract.

The ordinary web owner can open the changed decision, read current source text,
status, paths, author and revision, deliberately check the source-review box, and
sign the existing answer or correct it. The request submits exactly those shown
`source_evidence` and `source_decision_pins`, with `expected_updated_at`. All are
compared atomically. A change after the readback refuses confirmation and asks
for a fresh review. The action retains source dependence; it does not require
claiming that the answer is independent. When evidence changes, old co-signatures
are not reused. When that review replaces source versions on a standing rule,
the existing reusable grant is retired in the same writer transaction. The
source readback and web form display this effect before confirmation. A sign-off
with unchanged answer and source versions keeps its grant; recording a new
human answer or publishing a replacement source-derived answer requires a fresh,
explicit make-rule action before future automatic reuse. Old signatures,
immutable decision/source snapshots, and completed proof bundles are preserved.
The browser regression is `tests/browser-context-memory.cjs`.

The shared Slack/Teams consumer displays these same complete snapshots (or
directs the owner to the authenticated review page for an oversized reading),
saves the exact pins with its held proposal, and submits them only after
deliberate, generation-bound confirmation. Ordinary `yes` never refreshes source
heads. The combined readback renders the complete root scope once: a source
review already contains it, while an ordinary approval uses the structured
scope renderer. Data is escaped once in its final display context, and the
complete source/root/action/effects/confirmation-code prompt must fit both the
12,000-byte UTF-8 ceiling and 50 Slack blocks. Oversized proposals are cleared
and redirected to authenticated web review, never shortened into consent.
See the chat confirmation contract below.

Source invalidation writes a durable `source_review_requests` entry in the same
transaction as the new head and invalidation. The delivery worker consumes it
after commit, queues the existing review notification, and sends outside the
write lock. No-op imports create no review request. Source-change epochs allow
another material change to request another review while deduplicating retries.
Generic import also returns `affected_decisions`; native ingest uses the same
durable queue, so detection is not limited to a host remembering a return value.

### Completed precedents and fresh work

A completed source-backed decision retains its actual historical signatures,
`signoff`, and immutable proof when an underlying source changes. Its current
applicability is marked for review, so a standing rule cannot authorize fresh
reuse. Traversal continues through that completed node to active consumers in
other tasks; it never stops at the completed task boundary.

A current-source readback labels completed dependencies `historical: true` and
binds their exact `updated_at` in `decision_pins`. A human can explicitly review
all displayed current premises and approve the active task without editing or
resigning the completed predecessor. That approval retains the completed
precedent as a version-pinned historical relationship, while the active task
gets its own current source pins and human sign-off. Future changes to those
current premises still invalidate the active task. Generic yes cannot perform
this transition. `historical_signatures` in the tree distinguishes the signed
historical fact from current `authorized`/`needs_review` applicability.

### Slack and Teams current-source confirmations

Both verified chat transports now use the same generation-bound source-review
consumer. A source-backed `answer: ...`, an answer with a reason, a natural-language
answer readback, or `sign off` produces a held proposal rather than refreshing
source heads immediately. It displays the whole proposed answer, decision scope,
source decisions (including historical status), and complete source snapshots
with their exact version IDs. Model output cannot select these pins or request
independent replacement. Snapshot text is quoted and platform markup escaped;
its content cannot mention extra recipients or execute a confirmation.

The server stores those displayed snapshots, `source_evidence`,
`source_decision_pins`, and `expected_updated_at` with that unique proposal. The
owner must reply with its exact `confirm <code>` (or `yes <code>`) after delivery.
A bare yes does not refresh sources, even when replying directly to the delivered
reading. The existing authenticated actor, original message occurrence, thread,
and delivery checks remain in force. Consuming the reading, recording consent,
and applying the answer/signature share the same write transaction. A changed
source, source decision, or reviewed decision revision refuses confirmation.
Retries never replace the source pins of a previously generated proposal.

A complete source reading has a conservative 12,000 UTF-8-byte chat budget,
including escaped presentation and confirmation instructions. Oversized source
sets, unavailable sources, and namespace/decision blockers produce no confirmable
partial reading. The reply points to the existing authenticated task review path
(`/#runs/<task>`; absolute when the instance public URL is configured), where the
owner can resolve the blocker and review all sources. It creates no access token
or new platform button endpoint. Explicit independent replacement remains the
separately labeled web/API action; an answer merely containing “independent” does
not retire its sources. Co-signers review fresh decision revisions separately.
Completed predecessors retain their historical signatures and proof while the
active child receives its own reviewed current source pins.

`tests/test_source_review_readbacks.py` exercises synthetic Slack and verified
Teams callbacks without real messages, provider requests, or model calls. The
same cases are registered for the PostgreSQL loader.

### Private conversation about attached sources

Natural-language questions and chat can use the source snapshots already
selected by the owner's current source review. Intent is interpreted before
adding the new bound-source projection. Existing task and conversation history
remain unchanged and may include earlier confirmed source readbacks; this is
not a claim that historical source text is absent from the initial request.
Before a response-only reading receives source text,
Raven refreshes the decision projection and reuses the existing full-readback
disclosure boundary: `authz.check` with `answer` for a pending decision or
`correct` otherwise, using the concrete delivery actor. The actor-neutral
`Store.get_decision` projection alone is not permission to disclose sources.
The internal-caller exemption is not used. This adds no eligibility rules and
does not invoke an answer, correction, proposal, signature or other action.

The response pass receives complete entries from `source_revalidation`, including
exact source versions, status, body, metadata and source-decision applicability.
`previous_version_id` and `changed` distinguish the old attached version from a
current review candidate; discussing the new version does not adopt it. Source
`source_updated_at` remains the provider-supplied update time, not a claim that
the provider was just fetched. Observation-only refreshes that leave the existing
review projection unchanged do not invalidate the reply. There is no arbitrary
document fetch, source search fallback, source refresh or access expansion here.

The added `bound_source_review` projection is limited to 24,000 encoded JSON
bytes, using the same escaping as the model payload. This limit is for the added
projection, not the entire pre-existing task/conversation request. Whole source
or dependency entries that do not fit are omitted, with explicit counts and
`complete`/`truncated` flags; bodies are never silently shortened. Oversized
review notices are explicitly omitted, not rewritten into a substitute notice.
The original review projection is unchanged. If the existing review is
unavailable, the source/dependency text is withheld and its bounded notice is
preserved. An unavailable bound review also suppresses transient Slack search
for that reply, even if an interaction token is supplied; search is not an
alternate route around its blocker. Ordinary Slack search without a bound review retains
its existing permissions and ephemeral handling. Available bound evidence may
share a response pass with those authorized transient results.
Server-written reply notices disclose these limitations and direct
the participant to their existing task review link for the complete review.

The response-only pass can return only question/chat. Source text is untrusted
evidence; instructions inside it cannot choose an action, supply approval, change
roles or make a rule. Before returning, Raven rechecks both the same disclosure
gate and the current decision/source projection. A material change discards the
source-informed reply. The refreshed response payload and its final recheck
include current authorization, review, sign-off and speaker-signature state,
which can change without changing the answer/scope revision. This only keeps
the explanation current; it adds no signing or eligibility logic.
Gate refusal returns only the original, unenriched
clarification; it does not try another source retrieval route. Ordinary private
questions add no task note or sign-off. Source-informed replies use the existing
ephemeral delivery path, so their text is not saved in conversation history,
webhook receipts or the reply retry queue. After a send failure, the queue holds
only a request to ask again for current sources. Existing confirmed source
readback storage and consent rules remain separate and unchanged.
If the response reading fails, its diagnostic contains a fixed failure label,
not external error text that may echo source content. The failed explanation
also leaves any pending confirmation readback intact. Initial intent-reading
failure and repair behavior remain unchanged.

Normally this adds one logical model reading for an eligible source-backed
question/chat: the initial `slack_conversation` interpretation without the new
bound-source projection, then the
response-only `slack_conversation_sources` reading. The latter uses the existing
fast model and 1,600-token requested output budget. It contributes to the existing
process-level `USAGE` call/token totals; this is not a new durable billing ledger.
Existing client retries and the intent repair budget still apply, so provider
attempts may exceed the logical reading count. If transient Slack search results
are also supplied, they share that response pass rather than requiring a third
logical reading. Source-free conversations, direct shortcuts and human action
interpretations do not acquire a source-response pass.

`tests/test_conversation_sources.py` uses the existing synthetic owner fixture
and mocked disclosure refusal, source reads and model outputs. It checks exact
projection, status/version/applicability fields, both disclosure checks, changed
and unavailable sources, blocked search fallback, size omissions, response-only actions and private
non-retention. These are deterministic control tests, not evidence of live model
fidelity, provider permission correctness or broader participant eligibility.
They do not review or change identity resolution, required-approver rules or
owner-link isolation. The module is registered with the PostgreSQL loader;
registration alone does not claim a PostgreSQL run.

`tests/test_legacy_source_reuse.py` adds synthetic legacy migration/restart,
auto-rules-on, malformed/partial provenance, immutable proof, current-source
review, independent replacement, and active-dependent gates. It is registered
with the PostgreSQL loader; provider and live transport access are unnecessary.
## Exact external-reference handover

A fresh host does not need an internal `record_id` or an import write to find a
previously stored work item. `bridge_lookup_record` and authenticated
`GET /api/records/lookup` use the same read-only implementation. Supply `repo` as
the exact stored repository key (for example `synthetic/platform`), and exactly
one of:

- `external_id`: the stable provider object ID, with `provider`, `namespace`, and
  `object_kind` to select its canonical identity; or
- `ref`: the exact current human-readable display ref, optionally narrowed by
  those same identity fields.

Example MCP arguments for a Jira handover:

```json
{"repo":"synthetic/platform","provider":"jira","namespace":"synthetic-site-a","object_kind":"jira","external_id":"opaque-123"}
```

The equivalent REST request is
`GET /api/records/lookup?repo=synthetic%2Fplatform&provider=jira&namespace=synthetic-site-a&object_kind=jira&external_id=opaque-123`.
A host that only knows `POL-7` can supply `ref=POL-7` instead of `external_id`.
There is no fuzzy text match, URL parsing, basename fallback, installation
inference, provider fetch, or new connector permission. `client_key` is never
an external work-item identity. The old `bridge_get_record` and exact
`GET /api/records?repo=...&record_id=...` content/history APIs are unchanged.

Lookup outcomes are explicit:

- `matched`: one exact stored record, its matching identities, latest observed
  source metadata, bounded version metadata, and typed links below.
- `ambiguous`: multiple records, bounded identity-only candidates, and explicit
  disambiguation fields. Repeat with a selected candidate's exact identity.
  Newer imports never win. Inaccessible/deleted duplicates still participate in
  ambiguity; availability cannot silently pick another installation.
- `unknown`: no exact stored match, or the matched record has no readable head.
  This makes no claim about the provider's contents or access.
- `unavailable`: the exact source is deleted/inaccessible; retained versions,
  lifecycle details and task/decision links are withheld, as with exact-record
  source content reads. This is not an external ACL synchronization mechanism.

Provider, installation/workspace, kind and stable external ID remain distinct.
Two Jira sites or `jira`/`ticket` kinds may share a display ref and external ID.
GitHub `issue` and `pr` stay separate. Native GitHub PR external IDs use the
stored `owner/repo/pull/number` identity; the display ref may be just `number`.
Slack message IDs must include the stored channel/message identity, with the
workspace as `namespace`; a timestamp alone is not expanded or searched.
Explicit identities learned by native ingest can select the same stable record
without becoming duplicate candidates. A stable external ID survives a display
ref rename; the previous display ref is visible in old version metadata but is
not silently added as an alias for current-ref lookup.

A matched result distinguishes these relationship lists:

- `task_anchors`: explicit `work_item` or `context` anchors, task lifecycle,
  pinned source version, and latest observed source version.
- `decision_sources`: exact version-pinned source edges. `decision_premise`
  means an active supporting/contradictory edge, `decision_association` means
  an active non-premise edge, and `decision_history` means a retired edge, a decision on a completed/abandoned task, or a
  withdrawn/adopted/duplicate/superseded decision. `role`, `edge_active`, `historical`,
  `decision_version_id` and `stored_stale` retain those separate facts.
- `task_decisions`: decisions belonging to explicitly anchored tasks, labeled
  `task_anchor_association`. This does not assert that any such decision used
  the work item as evidence. No evidence edge is fabricated by lookup.

Every relationship includes the pinned source version and the latest observed
source version separately. `pin_matches_latest_observed` reports equality,
not policy applicability. Decision status/sign-off/review flags and `decision_superseded_by` are explicitly
current row metadata, even beside historical source edges; they do not rewrite
or restate an old decision snapshot. Follow returned task/decision handles with
`bridge_get_tree`/`bridge_get_decision` for scope and authority. Association,
source state such as Done or Closed, and a successful lookup never authorize
anything or acknowledge an agent's decision read.

`latest_observed` and `versions` expose the actual stored source `status` and
`resolved` plus source/observation timestamps, supplied sequence/version and
observation provenance. Local sequence orders observations only; latest observed
source state is not currently adopted policy. Source snapshots, message and
notification bodies, authors, audience metadata, connector locators and dedicated
URL fields are excluded from this index. If an identifier itself is a URL with
user information, query parameters or a fragment, that output field is blanked
and listed in `redacted_fields`; matching still uses the untouched exact input
and the blank must never be treated as a replacement identity. Supported exact-record reads are separate when source
content is needed and available.

`limit` is an integer from 1 to 100 (default 25), applied independently to every
returned list, including candidates and matching identities. Each list returns
`items`, `limit`, and an explicit `truncated` flag. Versions are newest observation
first; candidate order is deterministic identity order, never recency. A single
shown candidate with `truncated: true` is still ambiguous. This bounded lookup
is an index rather than an exhaustive graph export; narrow identity filters or
follow the exact task/decision/record APIs for further detail. Reads use one
consistent database snapshot, make no source/history/authority/proof writes,
and do not acquire a publication writer lock.

`tests/test_source_lookup.py` exercises actual authenticated HTTP MCP and REST
with a read-only viewer host, synthetic Jira/GitHub/Slack identities, duplicates,
renames, native identity aliases, old pins/new heads, lifecycle/availability,
repository isolation, metadata omission, explicit bounds, restart and immutable
history/proof checks. It is registered with the real PostgreSQL test loader;
provider calls are not required.

### Link an existing work item

Reading or looking up a source does not attach it to a task. After an exact
`bridge_lookup_record` match, read the selected record with `bridge_get_record`,
then call `bridge_link_work_item`. The equivalent authenticated REST operation
is `POST /api/tasks/{task_id}/work-items`, with the same fields except that
`task_id` may be omitted; a supplied ID must match the URL.

Copy `record_id`, `source_version_id`, `repo`, `provider`, `namespace`, and
`external_id` from that read's `source`; pass its `kind` as `object_kind`.
Alternatively use its exact current `ref` instead of `external_id`, never both.
Ref collisions are refused even when an opaque record ID is also provided:
use the selected source's stable external ID to disambiguate. IDs and namespace
selectors must agree. No newest match, alias guess, prose extraction or silent
reimport occurs. Legacy sources also expose the required canonical fields.

`role` is `work_item` by default, or `context`. Both are informational task
associations. They do not create decision premises, signatures, approval,
read receipts, notifications, or human authority. Decisions on the task are
returned by lookup as `task_anchor_association`, not `decision_premise`.
The existing `bridge_settle_node(source_evidence=...)` route already accepts
`role: "work_item"` (or `"context"`) with exact record/version pins, without
reimporting. That creates a **decision-level association** while recording an
unsigned answer proposal that still needs human sign-off. It does not create a
task anchor. Its `work_item` and `context` roles remain informational; only
`support` and `contradiction` are premises. The new `bridge_link_work_item`
operation is for **pure task context**: it leaves the decision answer,
revision, source edges, and sign-off state untouched. Both routes remain
supported, and lookup distinguishes `decision_association` from
`task_anchor_association`.

The new link operation checks the exact task repository, canonical identity,
current version, source availability and retrieval freshness in the same
SQLite/PG writer transaction as the insert. A changed head requires a fresh
read. Existing task anchors establish each provider's selected namespace set;
the new operation refuses to widen that provider to a different installation.
An explicitly selected different provider may be added as context. Existing
associations and decision dependencies remain. A successful retry on an open
task against the same current pin returns `changed: false` without another history event. A
newly read version is an additional historical pin, never a replacement.

The new operation rejects completed and abandoned tasks. This is a boundary
of this API, not a newly universal restriction: existing `bridge_import_record`
with `task_id` and `anchor_role` retains its prior behavior, including explicit
multiple-namespace anchoring. Old completed rows, decision snapshots,
signatures and saved proof bundles are not migrated or reconstructed.

`facts.work_item` is a structured declaration, not a graph link. Task kickoff,
tree, individual node/decision and task history reads expose
`work_item_association` with `declared`, `status`, `record_ids`, `links`,
`historical`, and `notice`. Each informational link carries its exact identity
and pin plus `origin: "task"` or `"decision"`; a decision's work-item edge is
never relabeled as a task anchor. Supporting evidence is excluded from this
association list. Tree nodes include the link identities even when the task's
anchor list is returned separately.
Matching uses exact equality against already explicitly attached work-item
records' stored `ref` or `external_id`; it does not search or infer new pins.

- `unlinked`: the named ID has no matching explicit work-item association.
- `linked`: matching pins identify one record; their versions can be historical.
- `ambiguous`: matching associations identify distinct records.
- `undeclared`: there is no structured ID. Existing anchors remain separately
  visible; arbitrary question/context/client-reference text is not parsed.

A `context` anchor cannot satisfy a declared work item. Decision reads may
also recognize their own existing active `work_item` source edges; duplicate
nodes never borrow another task's association. Changing the declaration does
not remove old anchors or evidence. Unlinked state is visible context, not a
new completion or approval gate. Task pages, owner decision history and personal
briefs show association separately from source evidence and authority.
On completed or abandoned tasks, unlinked or ambiguous declarations are
explicitly historical and direct new work to a new task, not to the closed-task
link operation. Namespace checks use each anchor's pinned canonical snapshot;
native git-to-GitHub alias upgrades cannot hide an existing namespace selection.

`tests/test_work_item_links.py` covers the supported lookup/read/link flow,
MCP/REST parity and permissions, ambiguity, stale/current versions, wrong
scope, concurrent retries and source races, existing imports, declarations,
and signature/proof preservation with entirely synthetic offline inputs.
`tests/browser-work-item-links.cjs` exercises actual Chromium against a local
synthetic fixture: MCP lookup/read/link, REST retry, task and owner views,
unlinked-to-linked display, retained historical pins, and no approval changes.
