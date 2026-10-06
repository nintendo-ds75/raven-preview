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
