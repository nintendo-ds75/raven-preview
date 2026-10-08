# Current rule reuse before contact clarification

A learned contact can have historical facts that a new request does not state.
Raven still asks for that missing routing scope before sending a question.
However, when a current standing grant already authorizes the request, no new
contact is necessary merely to repeat that historical scope.

Before returning a contact-only clarification, Raven attempts the existing
deterministic cache and node-placement path inside a writer savepoint. The
attempt uses only stored evidence: no model, live connector lookup, background
worker or notification runs. It keeps a node only when the final result is
rule-authorized under the current facts, repository, source versions and
freshness, grant scope and expiry, workspace policy, and existing required
approver checks. It does not create a new human signature or standing grant.

A broad grant can cover a different or undeclared work item when its explicit
conditions do not require that identity. Missing customer, environment or any
other required condition remains unknown. An own-scope grant cannot use missing
historical facts to prove that this is the same scope. Foreign source namespaces,
expired or ended grants and stale sources do not qualify.

When the attempt is not qualified, its draft nodes, source snapshots, events and
client-reference claims are rolled back, including under an existing caller
transaction. The original clarification remains, with no question sent. An
unfinished competing client-reference claim is not awaited while holding the
probe writer. Blank claims older than the ordinary 120-second recovery window
are recovered inside the savepoint; an unqualified attempt restores them.
An already-published idempotent result retains its existing facts notices and
background state. A successful rule result is published once through the normal
node placement path and obeys any outer caller rollback.

This changes the order of cache reuse and contact discovery. It does not relax
the contact-learning scope rules, infer current facts from history, or rewrite
earlier failed or signed decisions. Finite retrieval limits remain those in
[rule candidate selection](rule-candidate-selection.md).
