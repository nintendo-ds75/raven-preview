# Context and host mechanism validation, 6 October 2026

This implementation extends `bdd83b7`. It adds working product mechanisms to the
previous evaluation-audit foundation. It does not claim that Raven is perfect or
that a new live coding-agent experiment completed.

## Shipped behavior

* An optional Airweave adapter supplies hybrid retrieval and connector-managed
  source synchronization. Retrieved excerpts enter Raven's existing versioned
  source/decision graph. Static context and human answers remain connected by
  exact source versions, task anchors and decision dependencies.
* Source relationships nominate prior signed answers even when the new question
  has little lexical overlap. Relevance, scope and fresh approval still matter.
* Five-minute retrieval leases, source-change invalidation, explicit access
  denial and permission checks prevent stale evidence from silently authorizing
  work. A failed refresh is unavailable, not a fabricated deletion. Known access
  denial also blocks full-source reads. Jira resolution survives import.
* Claude/Codex adapter hooks verify Raven, register the exact first prompt once,
  keep follow-ups on that task and recover interrupted registration. Concurrent
  hook calls cannot create multiple tasks for the same binding. Attribution is
  to the authenticated credential, not an independently verified keyboard user.
* A durable, leased mailbox delivers human/source changes to an opt-in local
  supervisor. Late corrections and asynchronous finish readings remain visible
  after completion. No source-supplied command is executed, and normal host
  permissions remain in force. Delivery is at least once, not exactly once.
* A local finish helper captures Git's actual diff bytes from an explicit base,
  including already committed changes. It refuses untracked files. It does not
  independently certify test claims or that the chosen base is correct.
* Personal task links now support complete source revalidation. A recipient can
  handle a review longer than Slack's limit without creating an account. Current
  source and decision revisions are still checked atomically on confirmation.
* Task history shows retrieved queries and host prompts. Connector state and
  tool inventories are available through MCP. Inventories of other MCP servers
  remain reported capabilities, not assumed permission grants.

See [context setup](context-connectors.md), [host setup](host-adapters.md), and
the [frozen replay/audit tools](../evals/audit/README.md).

## Failures found during verification

1. Expired context initially disappeared from retrieval while the finish gate
   still accepted its signature. The gate now checks source leases directly.
2. A transient outage should block use, but should not manufacture a new source
   version or demand a new signature once identical accessible evidence returns.
   Actual access/grant changes still invalidate the approval.
3. A completed task initially suppressed late owner corrections and delayed
   diff readings. The mailbox now considers events after completion.
4. A diff helper defaulting to HEAD omitted already committed work. Finish now
   requires the starting commit explicitly.
5. The full suite found a missing lifecycle-handler registry entry and an extra
   decision-table scan in workspaces without connectors. Both were fixed.
6. The sealed urllib3 checkout exposed colliding contributor aliases during
   pseudonymization. Evaluation preprocessing now coalesces those unreferenced
   engineer rows while retaining the graph's name/email links and source IDs.
7. The old walkthrough expected twelve tools, even before this change. Its
   expectation now includes all seventeen actual protocol tools.
8. Final review found that collection-level access denial needed to invalidate
   the cached grant immediately, and direct record reads also needed to honor
   expired/revoked retrieval. Both paths now reject stale access. A Jira
   `Won't Do` resolution is carried into the record instead of being discarded.

The failed runs informed these fixes; they are not counted as uninterrupted
success. Machine-readable scope and result summaries are in
[the evidence directory](../evals/results/context-host-mechanisms-2026-10-06/README.md).

## Recorded checks

* Production-format image: 1,716 tests run, zero failures, 132 skips. The first
  candidate had two failures, fixed before that rerun.
* Broad PostgreSQL suite: 1,190 run, zero failures, 113 skips.
* Final source-access, hook-recovery and review changes: 157 focused tests,
  zero failures or skips; 118 PostgreSQL tests, zero failures, two checkout-only
  skips. These final targeted checks followed the aggregate run.
* Five Chromium suites passed, including 70 personal-task-page steps across
  desktop and phone widths and accountless complete-source review. Four DOM
  suites also passed. Speech APIs were mocked, not real microphone tests.
* Frozen urllib3 scripted walkthrough: 37/37. The alias-preprocessing and stale
  protocol expectation failures were fixed before this result.
* Fresh final Docker/PostgreSQL startup passed authenticated MCP initialization,
  seventeen-tool discovery, idempotent kickoff, exact prompt retention and finish.
  The successful check used the generated limited agent credential.
* Documentation and fixture checks passed in the Git checkout. Optional provider
  dependencies, opt-in suites and checkout-only cases account for image skips;
  skipped cases are not counted as passes.

## What this does not establish

The Airweave transport was simulated from its published OpenAPI schema. No
customer Airweave installation, connector OAuth flow, real group ACL expansion,
upstream deletion or revocation propagation was certified. The operator must
scope the collection to the entire Raven workspace and maintain its reader
mapping. The lease permits bounded staleness plus upstream synchronization delay.

The installed Python hook really ran as a separate process over authenticated
MCP HTTP. The supervisor's coding process was substituted in failure/retry tests.
Actual Claude/Codex hook loading and a resumed coding process were not certified
here. The locally installed Claude CLI reported no active login, and this run
used no inference credentials. ChatGPT Work MCP Events remains unimplemented.

The urllib3 walkthrough uses real frozen Git history, pseudonymous identities,
simulated Slack and a scripted MCP host. Its 37 checks validate the workflow;
it does not implement a new urllib3 feature or measure model question quality.
The prior unresolved signer-identity review remains unresolved.
