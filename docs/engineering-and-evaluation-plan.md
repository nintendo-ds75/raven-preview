# Make Raven reliable to connect, understand and evaluate

The shortest path is to keep Raven's existing source/decision graph, make the
host lifecycle explicit, and evaluate the whole interaction under a controlled
information boundary. Do not build another Glean before a team can complete one
task comfortably through its coding agent and Slack.

This document separates implemented evaluation tooling from proposed product
work. It is not a claim that hooks, a new connector platform, or automatic host
wakeup have shipped.

## 1. What exists and what this change adds

Raven already has Git history ingestion, GitHub synchronization, Slack directory
and conversational delivery, record imports, versioned external sources, linked
human decisions, source-change invalidation, task history and saved proofs.
`bridge/context_memory.py`, `bridge/source_lookup.py`, `bridge/canvas.py:trace`
and the task UI are the foundations to extend. The graph is already shared;
external evidence and interactions should not become two competing knowledge stores.

New in this change:

* `python -m evals.audit freeze` creates an independent pre-cutoff Git object
  store, a hashed task brief with an attributed leakage review, and frozen source
  snapshots with version/time/audience metadata.
* The evaluator can freeze a rubric before host calls. Contact correctness is a
  set of justified people, with separate knowledgeable/referrer/decider roles.
* The replay MCP proxy records redacted request/response observations in a
  serialized hash chain. The existing remote replay runner adds prompts,
  evaluator interventions, newly observed internal events, changed decision/source
  snapshots and final task history to the same audit.
* An offline viewer shows calls and the latest observed source/decision state at
  a chosen event. It never consults the current database while showing the past.
* An independent patch check compares submitted bytes with the actual checkout,
  rejects untracked work and checks that the patch applies to the declared base.

These tools do not provide OS isolation, authenticate an evaluator's claimed
identity, inspect private model reasoning, or automatically grade question quality.
The legacy replay runner gains observations; it is not retroactively certified
as a sealed or blinded run. The [tool guide](../evals/audit/README.md) specifies
the remaining runner obligations.

## 2. The customer experience to optimize

One operator connects a GitHub repository and installs a Slack bot. Raven checks
actual readable scope, directory availability, a test DM/reply, and host-tool
connectivity. It presents a short inventory: available, missing permission,
not connected, or failing. No ownership spreadsheet is required.

The developer continues in the coding host. The host discovers relevant paths
and records uncertain decisions. Raven suggests a first contact using permitted
static evidence, asks a compact question, accepts a referral, and learns a scoped
contact. A learned contact is not an authorization grant. Slack recipients should
finish ordinary replies and source revalidation without opening a full account.
The previous live run's oversized-review fallback remains a concrete blocker.

Offer the stakeholder a read-only task view with the requester, who has been
asked, each answer and its revision, sources read, unresolved questions and the
next action. Restrict it to the task and the viewer's access. Do not expose a
workspace-wide export or all retrieved documents through a task link.

Measure resistance in real terms: operator setup minutes, number of screens,
human reading time, unnecessary questions, unique people interrupted, handoffs,
corrections and time until a usable answer. A better retrieval score is not a
win if it creates more coordination work.

## 3. Deterministic host lifecycle, without inventing a universal MCP wakeup

An MCP directory or skill makes tools discoverable, but does not guarantee that
the model calls them. Use a supported host adapter with recorded lifecycle events.

| Event | Adapter responsibility | Model responsibility |
| --- | --- | --- |
| Connect/session start | Initialize MCP, list tools, verify version/capabilities, bind authenticated principal and host session. Fail visibly if required Raven is unavailable. | Learn the available operations and stated limits. |
| First task prompt | Register the exact user's request once with a stable client key; bind later prompts to that task or explicitly start another. | Inspect the checkout and discover decisions, without asking users to provide paths. |
| Tool/discovery result | Persist the exact observation and evidence versions before returning it. | Form a useful question and distinguish fact, prediction and missing judgment. |
| Waiting | Save task/thread IDs and event cursor; use bounded wait while the host exists. | Continue independent work; do not approve its own guesses. |
| Human event | Persist an outbox event; deduplicate by host/session/event ID; queue only the authorized session to resume. | Read current answers and corrections, then act. |
| Finish | Capture diff bytes directly, record actual test process results, compare the checkout and proof; enforce recorded decision gates. | Explain remaining uncertainty and implementation. |

Current official Codex documentation provides `SessionStart`, `UserPromptSubmit`
and tool hooks. Its app-server can reopen a thread and start or steer a turn.
Use those supported controls rather than assuming a server notification restarts
a closed coding client. A required MCP configuration can fail startup when Raven
is unavailable. See [hooks](https://learn.chatgpt.com/docs/hooks) and
[app-server](https://learn.chatgpt.com/docs/app-server).

For **ChatGPT Work**, OpenAI also documents an actual
[MCP Events subscription API](https://developers.openai.com/plugins/build/mcp-events):
`server/discover`, `events/list`, `events/subscribe` and `events/unsubscribe`.
An authorized subscription receives signed webhooks, which can trigger asynchronous
work. This is the appropriate Raven adapter to investigate for that host. Bind
subscriptions to the authenticated person and task, revalidate access, and keep
stable event IDs, expiry and retries. Receipt does not mean the agent has finished
processing. This API is not a universal way to wake a closed Codex or Claude CLI.
No MCP Events endpoint is implemented in this change.

For Claude Code, implement the analogous adapter against its documented
[hooks](https://code.claude.com/docs/en/hooks) and supported session-resume API/CLI.
Test actual installed host versions. Hooks run only when their host fires the
event; they are not themselves a durable scheduler. A local supervisor or customer
worker owns resume credentials, workspace binding, retry/backoff and consent to
background execution. Never let source text name a shell command to execute.

Before releasing either adapter, require tests for startup without Raven, prompt
retry, concurrent prompts, compaction, interrupted wait, duplicate Slack event,
late reply, moved checkout, stale session, and a customer disabling background
resume. A prompt hook must not create a new task for every conversational reply.

## 4. The context layer: reuse connectors, keep one authority model

My recommendation is **native GitHub + Slack first; evaluate Airweave as an
optional retrieval/connector backend; support existing Glean rather than requiring
replacement**. Jira can initially use the explicit record-import adapter, then a
poller/webhook with the same source contract. Uploaded docs and a bounded set of
customer-selected URLs use that contract too.

Airweave is focused on unified retrieval and incremental source connections, has
an [MIT-licensed repository](https://github.com/airweave-ai/airweave/blob/main/LICENSE),
and supports an API/MCP integration. Its Slack connector uses live federated
search with user-scoped `search:read`, not a bot-token archive. This makes it a
candidate ingestion adapter, not a drop-in implementation of Raven's Slack bot,
approval semantics or permission model. Validate deletions, access revocation,
source-version fidelity and per-user isolation before using it with customer
data. [Concepts](https://docs.airweave.ai/concepts),
[Slack connector](https://docs.airweave.ai/docs/connectors/slack).

Onyx is the closer complete Glean-like search product. Its connector documentation
places automatic source-permission enforcement in its Enterprise edition. It is
not the smallest default dependency for a Raven pilot, and the open-source label
alone does not establish that the required access controls are included.
[Onyx connectors](https://docs.onyx.app/overview/core_features/connectors).

Slack has not universally disabled ingestion. Its history/replies limits depend
on app distribution; internal customer-built apps retain their existing tier.
Search permissions differ from bot messaging permissions. The newer
`assistant.search.context` also has its own bot/action-token requirements.
Probe the actual installed capabilities instead of assuming one token does all
three jobs. [Rate-limit clarification](https://docs.slack.dev/changelog/2025/06/03/rate-limits-clarity/),
[search contract](https://docs.slack.dev/reference/methods/assistant.search.context/).

Every adapter should produce the same envelope:

```
workspace / provider / namespace / external object ID
source version + content digest
created, updated, observed timestamps; source status; deletion marker
permitted audience + authorization principal + permission version
body + source URL + source-authored identity
connector cursor, last successful sync, failure and staleness state
```

External source versions link to questions, proposed answers, human answers,
signatures and implementations in Raven's current graph. Store how a source
supports, contradicts or supersedes something, and the exact version read. A
cached interaction must retain those dependencies. A webhook, poll or permission
change invalidates current reuse; it must not rewrite historical proof bytes.

Treat live search as an observation with a retrieval time and access context,
not as a synchronized copy of the whole organization. Absence from one search
result is not a deletion signal. Restrict reuse to audiences that can access the
underlying sources; do not let a broader task page reveal a private Slack snippet.
When source access is unknown, do not return that content under another person's
credentials. This needs connector-specific validation, not a blanket claim that
the current workspace roles already solve cross-source ACLs.

Capability discovery is explicit: the host supplies its registered tool inventory;
Raven probes only configured connectors and requested read scopes. It can suggest
connecting a missing tool, but cannot discover or authorize arbitrary MCP servers
on a customer's behalf. Start onboarding with “here is what I can read and whom
I can reach,” followed by a small preview of imported context.

## 5. A defensible year-back replay

Set an absolute UTC cutoff and full base SHA, for example a selected repository
state on 6 October 2025. Do not just check out an old commit in a current clone.
Keep the evaluator's full clone, future patch, reviewer labels and rubric outside
the host and Raven containers. Expose only the sealed checkout and approved
historical source snapshots. Raven's ingest must use that same fixture, with live
GitHub/Slack/Jira sync disabled for the replay.

Enforce filesystem isolation and deny general network access to the coding host;
allow only the local Raven endpoint and configured inference transport through a
supervisor. A prompt saying “do not look ahead” is not isolation. Dependencies
must be pinned/cached and their provenance recorded. The evaluator and inference
models may know public code from training; this procedure excludes runtime future
access, not memorization in model weights.

A record created before the cutoff can still contain an edited body, later
comments, changed status, today's team membership or a post-cutoff review. Use a
historical snapshot of each object/field or leave it unknown. Never filter only
`created_at` and retain today's contents. The fixture validator rejects known
post-cutoff snapshot times, but cannot independently authenticate a timestamp
asserted by the fixture author.

For task `n+k`, prefer the original request available before implementation. If
that is unavailable, a separate editor constructs an outcome-only brief and a
blinded reviewer checks it. Bind that review to exact prompt bytes. Include the
symptom and user constraint, not the answer, owner names, future PR number, diff,
solution-specific identifiers or oracle tests. Legitimate task requirements can
be specific; do not strip the problem until it becomes impossible to understand.
Mark reconstructed tasks as such and report them separately.

Freeze the manifest and rubric outside the host before running. Record every
evaluator intervention and injected answer with time and actor. If a person only
becomes knowable after a referral, count that as recovery, not first-contact success.

## 6. Score the actual product, not commit-author prediction

| Dimension | Measured outcome | Important distinction |
| --- | --- | --- |
| Connection | Successful required MCP handshake/kickoff; duplicate task count | “Tool installed” is not “tool called.” |
| Context | Relevant evidence found, exact version cited, forbidden/future reads | Retrieval is not authority. Unknown history stays unknown. |
| Discovery | Recall of preregistered judgments; unnecessary questions per task | Match intent, not exact wording or known-answer keywords. |
| First contact | Person belongs to a pre-cutoff justified acceptable set | Author/merger is one observation, not the universal answer key. |
| Recovery | Hops and time to a knowledgeable/authorized respondent | Referrer, expert and signer are different roles. |
| Question quality | Necessary, answerable, sufficient context, neutral, readable, scoped | Six separate human judgments with cited trace events; missing judgment is unknown. |
| Conversation | Correct interpretation, non-answer handling, correction retention | Readback confirmation is not inferred from conversational friendliness. |
| Authorization | Appropriate current sign-off; stale/denied/out-of-scope refusal | A “good person” routing score does not validate signer identity. |
| Memory | Reused evidence with current source/scope; fresh approval or valid rule | Measure recall and unsafe reuse separately. |
| Implementation | Native tests, held-out behavior, exact applicable diff/proof | Host claims and an advisory model verdict are separate observations. |
| Experience | Human reading time, pings, account creation, latency, interventions | Supervised recovery is not an autonomous first pass. |

Build acceptable-contact sets from listings, historical reviews/discussions and
regional technical involvement available at the cutoff. Require a concrete
justification for each member; do not add people after seeing Raven's answer.
Use two reviewers on ambiguous examples and report disagreements. Keep authorized
signer labels separate, and leave them unknown when OSS cannot establish them.
Use customer shadow pilots to establish real organizational authority.

## 7. The improvement loop and release order

1. Freeze task/source/rubric versions and run a small development cohort through
   the audited transport. Include cold start, cache hit, source change, ambiguous
   ownership, no-decision work, long replies and restart.
2. Triage failures by stage and attach an exact event range, observed behavior,
   counterexample and expected outcome. A model may propose a grade, but its
   conclusion needs checkable observations or an attributed human adjudication.
3. Reproduce deterministically where possible, add a rejecting regression, fix
   the smallest component, and retain the failed run unchanged.
4. Rerun the frozen case under the same model/config. For stochastic behavior,
   preregister repeat counts and retain every attempt, timeout and recovery.
   Report rates with denominators and cost, not selected success screenshots.
5. Evaluate a separate untouched holdout cohort. Once a failure has informed a
   fix, that task is development data. Do not repeatedly tune against the final
   held-out test while calling it held out.
6. Compare no-Raven, routing-only and full-Raven runs under matched budgets.
   Promote only after correctness and human-effort checks hold. No automatic
   production edits or source mutation by an evaluation judge.

Implementation order after this tooling:

* **Next:** direct-byte patch submission/attestation, accountless complete source
  review, readable Slack readbacks and truthful inherited-provenance labels.
* **Then:** one supported host adapter with durable session binding and an
  idempotent resume queue; capability onboarding for GitHub and Slack. For a Work
  pilot use MCP Events; for a local coding-host pilot use hooks and a supervisor.
* **Then:** connector access/revocation contract and source freshness tracing;
  evaluate Airweave on a small controlled corpus before adding it as a dependency.
* **Then:** a customer-owned read-only trace export with explicit, previewable
  redaction and expiring support access. No automatic outbound telemetry.

For Lunabotics, ask Elizabeth for one repository, one test channel and permission
to install the bot, then run a guided task with the actual people. Historical OSS
replay develops the machinery; this small real pilot tests whether the ownership
and interruption assumptions hold. No outreach, customer access or installation
has been performed by this engineering change. The previously unresolved
signer-identity review remains separate work, not implicitly cleared by new traces.
