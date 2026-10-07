# Typed context for contact suggestions

Successor to the bounded contact-learning candidate `e0fb457`. This changes contact relevance only; it neither grants nor changes authority, signatures, required approvers, or eligible owners.

Match reasons and controls, defined before implementation:

- `namespace_conflict`: the same canonical provider has different known namespace sets. Exclude the historical contact, even if a different source relationship is shared. Positive control: another work item in the same provider/namespace remains a useful contact match.
- `material_fact_conflict`: explicitly stated organization (`org`/`organization`), customer, or domain differ. Exclude. Treat org and organization as aliases; never infer these values from names, URLs, text, or unrelated source records.
- `missing_source_namespace`: history is scoped to a known provider namespace that the current task has not established. Omit that learned contact and offer scope clarification through the existing clarification response. Historical values are possibilities, never copied into current facts or anchors.
- `typed_context_unknown`: history has no usable typed context, including legacy feedback. Retain an explicitly uncertain, lower-confidence positive suggestion when existing question/topic/path/fact gates allow it. Do not silently claim namespace compatibility. Uncertain negative evidence must not categorically suppress current contacts.
- `compatible_namespace`: known provider namespaces match. Exact work-item identity is not required for generalization; existing question/topic/path/fact rules still apply.
- `shared_work_item` / `shared_source`: matching canonical `(provider, namespace, object_kind, external_id)` relationships increase contact relevance only after all conflict/missing-scope checks pass. Evidence cites the source identity and recorded decision/observation chain. It conveys contact relevance, never expertise or approval.

Use available typed task anchors and active decision work-item/context edges through routing entry points. Internal task/decision IDs may retrieve these existing relationships. Direct routing callers may instead pass canonical source identities and explicit facts; no new opaque-ID requirement is imposed on a host. Unknown context remains visible. Original observation snapshots are not rewritten.

Synthetic controls will cover different-item same-namespace generalization, shared-item/source bonus with cited evidence, namespace and material-fact rejection, mixed-provider conflicts despite a shared source, missing-scope clarification without mutation, legacy uncertainty, mid-chain scope changes, and supported routing-entry propagation. Existing contact, conversation, and ordinary compatibility tests remain required. No providers or live messages are involved.


## Implemented comparison and score

A namespace match requires equal known namespace sets for each shared provider. A changed or partly overlapping set is conservatively a conflict. A historical provider missing from the current context is material missing scope: no learned contact is reused from that observation until the task establishes its actual namespace. Source providers present only in the current task make the old context partly unknown. `legacy` identities are unknown, not proof of a tenant namespace. Organization and `org` use one explicit fact key; conflicting aliases fail closed.

The score is `(question_similarity + relationship_bonus) * freshness_factor * context_confidence`. The maximum relationship bonus is 0.15 for a shared typed work item or 0.08 for a shared context source, never accumulated by repeated references. Known compatible context has confidence 1; unknown or partly unknown historical source/material context has confidence 0.65 and is explicitly labeled. Existing respondent-before-connector precedence remains. Unknown negative evidence does not suppress contacts in a newly known namespace/material scope. Explicit negative evidence continues to apply inside compatible known scope without requiring referral completion.

Task facts are snapshotted with new observations, and explicit decision facts retain their normal override. Later task edits never rewrite historical contact scope. Legacy snapshots are not retrospectively enriched. All historical fact keys keep their existing matching/clarification behavior, in addition to the typed namespace gates.

## Entry points and remaining limits

`route`, `route_ranked`, `rank_for_decision`, and `candidates` accept optional `task_id`, `decision_id`, or `contact_context`. `contact_context` can provide `anchors` as canonical provider/namespace/object_kind/external_id/role dictionaries plus explicit `facts`; record IDs are not required. Existing task/decision IDs retrieve already-stored anchors. Ladder contact ranking, task discovery, canvas contact selection, escalation contact ranking, reframing contact ranking, and scope clarification pass available current context. Existing host record import can establish a typed task anchor with its ordinary task ID and canonical source fields; no new host ID obligation is introduced.

The comparison does not infer organization/customer/domain from source names, URLs, prose, or a provider namespace. Cross-provider namespace equivalence is not inferred. Shared-source relevance does not prove that source content is current, that a contact is an expert, or that any approval is valid. Independent authority, signature-derived fallback, and eligibility mechanisms retain their existing semantics. This contract governs learned contact evidence only.


## Explanation and history-read limits

Historical alias contradictions report `material_fact_conflict`, never namespace compatibility as the reason for exclusion. Clarification reports missing source namespaces and missing material/scope facts before giving any relationship credit; `shared_work_item` and `shared_source` reasons and bonuses are withheld until those gates pass. This changes explanations without weakening the existing exclusions.

History reads currently load every observation for the repository before Python freshness checks. Measured temporary SQLite work increased with expired history; the [verification report](../evals/results/contact-learning-2026-10-07/typed-context/README.md) records the limited sample. No unbounded-scale claim, broad redesign, or silent truncation of potentially matching or contrary history is supported.
