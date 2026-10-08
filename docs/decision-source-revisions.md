# Decision source revisions and human corrections

A human correction or supersession commits together with invalidation of every
consumer reachable through `source_id`, parent/child relationships, or explicit
`depends` links. A failure rolls back the answer, supersession, affected
signatures, and audit events. Pending suggestions are withdrawn. Active signed
consumers need fresh review. Completed consumers retain their original signature
records and exported proofs; a review marker makes their current authority stale,
and traversal continues through them to active downstream work.

Plain sign-off and task completion recheck recorded decision-source revisions
inside their writer transaction. They do not rely solely on invalidation flags.
This includes managed execution publishing `result_ready` or `completed`; a
blocked publication becomes `review_required`. Read-back confirmation can own
the transaction: its consent record, signature, managed answer revision, and
invalidation either all commit or all roll back. Notifications and routing/twin
follow-up work begin after the outer commit. A delayed human-answer twin update
compares the captured answer with the live source before publishing any reuse.

## Existing database limits

`source_revision` is an ISO timestamp, not an immutable source version identifier.
Equivalent timezone encodings compare as the same instant. Missing, malformed,
naive-timezone, or mismatched pins are refused at sign-off/completion rather than
being backfilled from the source's current state. A responsible person must
review the current premises and record a fresh answer; simply signing the old
inferred answer is insufficient. A fresh signature on a human-authored node can
clear that node's own review marker after all its current recorded premises pass;
it cannot clear a stale source pin or an upstream review marker.

The existing schema has no historical revision pin on parent or explicit
`depends` edges. Their invalidation is atomic for supported human writes, and
validation follows their review markers and any recorded source pins
transitively. This change cannot reconstruct missing historical evidence or
detect an out-of-band answer rewrite that deliberately preserves its timestamp
and bypasses invalidation. It does not claim immutable, content-addressed source
versioning. Existing immutable exported proof bytes are never rewritten.

## Regression coverage

`tests/test_atomic_source_corrections.py` is provider-free and registered with the
real PostgreSQL test loader. It covers the two-connection commit gap, correction
against finishing/co-signing, rollback, completed intermediates, missing legacy
pins, timestamp encodings, source changes during completion, bound read-back
transactions, managed terminal publication, and delayed twin settlement.
Actual PostgreSQL execution requires the separately configured test environment;
loader registration alone is not a PostgreSQL pass.

## Read historical evidence without confusing it with current permission

`bridge_lookup_record` starts its matched response with `linked_decisions`: one
row per decision across the complete union of direct source edges and task
associations. Repeated historical edges cannot fill this list with copies of
one decision. The detailed `decision_sources` and `task_decisions` lists retain
their previous meaning and contents. Follow `linked_decisions.next_cursor` as
`linked_cursor`, with the same exact lookup and limit, while `has_more` is true.
This cursor traverses live links; restart after changes. Neither a task
association nor a source edge is approval. Read all relevant linked decisions
and their histories before claiming only one or no prior decision exists.

`bridge_get_decision` now defaults to a compact MCP projection capped at 32 KiB
of JSON-encoded text. Ordinary current-state fields retain their meanings.
`context_history` contains version metadata and explicitly named previews;
`historical_approvals` independently lists snapshots with recorded human
approval or rule use, so newer invalidation snapshots cannot bury them. This
is an index of recorded state, not signature verification or present authority.
Each collection has an exact total, offset, returned count and `has_more`.
The shared cursor advances both collections independently. Repeat the same
`decision_id` and `limit` with `pagination.next_cursor`; changed current content
or version identities invalidate the cursor even when timestamps are equal.

Large current fields are omitted whole and listed in
`projection.omitted_fields`; any retained snippet has a `_preview` suffix.
The encoded budget includes omission metadata and the first available entry
from each history/index collection (subject to `limit`). Labeled previews may
shrink further to fit; their complete full/exact-version read paths and original
lengths remain available. Large Unicode or escaped text must not hide the index.
Generic event and work-item histories have explicit counts and full-read
instructions. Previews and omitted fields are not complete approval evidence.
The separate `saved_revision` pointer identifies the recorded
`decision_revisions` row; its full answer/rationale are explicitly omitted from
the compact projection and remain available through `detail="full"`. This
historical signed revision is not part of `current_authorization`, and its ID
must not be supplied as a `decision_versions` ID.
These supported reads retrieve the complete material:

- `bridge_get_decision(decision_id, detail="full")` returns the former complete
  MCP body, without the compact response cap. It is intentionally potentially
  large. Existing Store and HTTP human-review reads remain complete by default.
- `bridge_get_decision(decision_id, version_id)` returns one complete immutable
  `historical_snapshot` and a separately labeled `current_authorization` block.
  Copy the exact `decision_versions` ID from either compact history list. A
  signed revision or proof revision is a different identifier and is rejected.
  The version must belong to this decision. The saved signoff, grant terms,
  citations and derivations remain exactly historical. `current_authorization`
  describes this stored decision now; it does not evaluate a new work item.

`version_id` and `detail="full"` cannot be combined with paging arguments.
Old calls with only `decision_id` remain valid, but consumers that require raw
`context_history[].snapshot` or complete event arrays must request full detail.

`bridge_search_decisions` keeps its previous behavior by default. Explicit
`mode="history"` searches current and saved question, answer, rationale and
context text, including needs-review, withdrawn and superseded decisions.
It returns bounded previews, exact-version pointers, current state separately,
and count/cursor pagination. It uses case-insensitive literal term overlap;
the response names the query terms used and any omitted query-term count.
This historical read calls the existing `Graph._memory_scope_sql()` visibility
helper unchanged, but is never used by candidates or automatic reuse. It makes
no new permission decision. Follow exact reads for approval evidence and let
the normal scoped node evaluate a new request's current facts and permission.

`tests/test_decision_reads.py` covers large histories, independent approval
discovery after 105 invalidation snapshots, saved rule uses, removal of generic
events, explicit omissions, complete reads, unchanged automatic search,
cursor binding, equal timestamps and concurrent writes. It is registered with
the PostgreSQL loader; a native pass does not imply a PostgreSQL pass.

The recorded-approval index counts saved snapshots, not distinct human approval actions.
A human-signoff snapshot, a used-as-source snapshot and a before-source-change
snapshot may retain the same signature or grant. Their separate version IDs do
not mean that a person signed or granted permission again.

The compact response cap is a transport-output bound, not a constant-work database
bound. Historical search evaluates the scoped current and saved text in SQL;
its cursor fingerprint streams scoped current fields and immutable version IDs
and fingerprints. Work therefore grows with the scoped history. Full historical
snapshot bodies are not deserialized merely to build a compact result page.
