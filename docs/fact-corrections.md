# Correcting a node's current facts

`bridge_add_node` retains its existing idempotency contract. An identical
`client_ref` returns the existing node. When a retry explicitly supplies changed
facts, the response includes `facts_applied: false` and `facts_correction` with
the supported repair tool and current revision. The repeated call changes
neither the facts nor their decision result.

Use `bridge_correct_node_facts` for an explicit pre-approval repair:

1. Read `bridge_get_decision(decision_id=...)` and review the current facts,
   evidence, and eligibility. Copy the opaque `fact_revision` exactly.
2. Supply `task_id`, `node_id`, `expected_revision`, a task-unique
   `correction_ref`, the complete replacement `facts` snapshot, and `reason`.
3. Read the resulting node. Its applicability and authority were reevaluated
   against the replacement facts, local current records, and the task's
   canonical repository. A correction itself is never human approval.

Example request, using synthetic values:

```json
{
  "task_id": "the-current-task-id",
  "node_id": "the-exact-node-id",
  "expected_revision": "copy-the-complete-fact_revision-token",
  "correction_ref": "ticket-facts-correction-1",
  "facts": "customer=sample,environment=test,plan=standard",
  "reason": "The current ticket explicitly supplies the previously omitted facts."
}
```

Facts replace the entire snapshot. Omitted customer, environment, or other
business facts become unknown, even if task facts or historical decisions
contain them. Empty facts text clears business facts. Repository conditions use
the task's canonical repository; contradictory `facts.repo` is rejected. The
operation does not change the question, context, repository, paths, human
identity or historical signed snapshots. Existing required approvers remain;
the ordinary publication helper adds any currently required approvers. If that
helper cannot establish the current set, the correction stays blocked for review.

Only active unsigned agent-origin nodes qualify. Merely queuing or sending a
notification does not count as a human action. Accepted conversation, a pending
read-back, or explicitly forwarded context counts as participation even before
a final answer. An actual reply, answer, signature, reassignment, reframing,
follow-up, or grant also does. Nodes that have
current or historical signatures or standing authorization require a human
correction in the inbox. Completed or abandoned tasks require a new task.
Existing historical authority never transfers automatically to a new task.

The opaque revision binds stored material, source pins, decision versions,
dependencies, and event generations, so equal timestamps do not defeat
optimistic concurrency. A stale token rejects the write; reread and review the
node before submitting a new request. An exact retry with the same
`correction_ref` returns the committed correction receipt without reevaluating
or applying it twice. A different request using that reference is refused.
`facts_correction.is_current` distinguishes the committed correction's revision
from a newer current node returned alongside its receipt.

The existing source-refresh guard is applied at publication and on every
correction response, including exact retries. `source_refresh_required` describes
the current guard; `applied_source_refresh_required` records it when the
correction committed (null for an older receipt that did not record it).
`applied_authorized` remains historical. A later blocked source or successful
refresh changes the current guarded view without rewriting that receipt, and
`is_current` also distinguishes changes in this refresh requirement.

Reevaluation runs in one local writer transaction, without model calls,
connectors, live retrieval, or scratch-node notifications. Before and after
immutable version IDs and fact snapshots are audited together. Errors roll
back the complete write. In-flight older model results lose their publication
check. Dependents are invalidated and old tree read receipts cannot acknowledge
the correction.

Existing exact source pins, decision dependencies, and review flags are
retained. Stale premises still require a person's complete current-source
review. Retained decision premises must also satisfy their declared applicability,
standing-rule conditions, and scope under the replacement facts. A current source
version alone does not establish that it applies to the corrected request.
Every retained external support or contradiction pin must also occur with the
exact record, version, and blocking role in the selected rule's current approved
source chain. Unmatched blocking pins require complete source review; context
and work-item pins remain informational. Source prose never proves coverage,
and canonical repository binding does not invent or change business facts.
A fresh match to an already-valid explicit standing grant can authorize
only when automatic rules are enabled, every existing required-approver check
passes, and retained sources remain current. The consumer receives no copied
human signatures. Otherwise it remains an unsigned proposal or open question.
