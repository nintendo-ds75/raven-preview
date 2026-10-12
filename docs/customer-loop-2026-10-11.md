# Customer loop on 11 October 2026

**The initial coding loop completed and produced working code. The full experience still needs supervision.** Two issues deserve priority before a customer depends on background operation: native source-access loss leaves old evidence usable, and a required follow-up on a completed task cannot be adopted. Slack-only use also failed for three ordinary owner answers because their source reviews exceeded the chat limit.

Tested production revision: `07e236ffaf6e9008c0c4e0d620b68989e8dc3380`. Main still pointed there when the run ended. Production code stayed frozen. This commit adds the report and evidence, not fixes.

[Evidence and audit instructions](../evals/results/customer-loop-2026-10-11/README.md). Times in the transcripts are UTC on 12 October; the run took place on the evening of 11 October in New York.

## What was real

A fresh `./setup` installation built the production Docker image, created a new PostgreSQL 17 database, enabled authentication, and ingested a real urllib3 checkout. Claude Code 2.1.296 used HTTP MCP with an agent credential and the installed project hooks. Raven's main and fast inference used the real Anthropic API, with `claude-sonnet-5` and `claude-haiku-4-5-20251001`. The browser walkthrough used computer control against that running instance. No manual ownership map was created for this live loop.

Slack Web API and signed Events API requests, Jira REST responses, and a documentation website were **synthetic HTTP services**. Their payloads went through Raven's actual connectors and delivery code. The directory contained 251 pseudonymous contacts derived from public history. No maintainer was contacted. Git history and blame were real; this was not a live GitHub App installation, OAuth flow, or webhook test. Airweave/Glean, Codex, Cursor, Teams and real Slack installation permissions were not validated here.

The optional browser owner pages required no Raven account. A separate synthetic member account was used to inspect task observability and add a follow-up.

## The historical boundary and task

The checkout was frozen at urllib3 commit `3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, strictly before `2025-10-11T00:00:00Z` by both author and committer time. It contains 4,299 commits from 410 distinct author email strings; the largest share is 19.07%. Email aliases mean this is not a count of distinct people.

The boundary verifier checked 22,449 objects, no future refs, no remotes, no shallow/promisor escape, no alternates and no submodules. The full clone and private oracle were outside the host mount. The host's internal Docker network allowed inference through an allowlisted proxy; a GitHub fetch destination was refused with 403. Model pretraining is not excluded by this boundary.

The agent received this ordinary request, without paths, owners, a decision list, or a solution:

> Our services sometimes receive Retry-After delays longer than they can afford to wait. Add an optional caller-controlled upper bound for Retry-After sleeping without unexpectedly changing existing retry behavior. Investigate the code, discover the policy and compatibility choices, obtain the needed decisions through Raven, implement the change, document it and test the edge cases. This is for the library-next release. No customer-specific policy is implied.

This is a **synthetic business task against frozen real history**, not a reconstruction of a held-out future change. The synthetic organization policies are separate scenario inputs, not alleged historical urllib3 policy. The generic freeze manifest calls the brief `blinded_reconstruction`; that label must not be read as evidence of a future-commit evaluation.

An oracle was written before the host ran and was not changed. Its hash is in `audit-heads.json`. It specified default compatibility, limit/zero/invalid-value behavior, raw parsing and clone behavior, and documentation expectations. Its contact set came from pre-cutoff involvement in `util`, not whoever authored a later patch. That set is not exhaustive proof of real organizational authority.

## Step-by-step result

| Step | Observed result | Remaining weakness |
| --- | --- | --- |
| Install and connect | Fresh Docker/Postgres setup worked. Real Claude project hooks registered the task before coding, from the ordinary request. | Tested Claude only. Hooks and an optional local supervisor must actually be installed and running; an MCP server cannot independently start an arbitrary host. |
| Ingest context | Native Jira, Slack and docs connectors imported two issues, one channel record and two document sections. Restricted Jira content and robots-disallowed docs were refused. Cancelled NET-102 did not become the adopted policy. | Workspace-shared imports do not provide per-reader enterprise ACLs. Loss of source access was not propagated to cached availability. |
| Discover decisions | Claude located `retry.py` itself and raised three related questions about the default, clamp semantics and raw parsing. | It did not obtain separate decisions for the full documentation requirements or numeric edge cases. Task facts remained `{}` despite a release in the prose. |
| Find a contact | All three questions had concrete git-based routing reasons and reached a synthetic Slack contact. A referral reached the selected runtime expert. | The first pick was outside the preregistered contact set. This scripted scenario needed a referral; it does not establish that the real contributor was the wrong person. The later zero-delay question again needed referral. |
| Converse and sign | One natural answer completed through Slack read-back and confirmation. A non-answer recorded no decision. The limited agent could not answer or sign. | Two initial answers, and the later zero-delay answer, required browser recovery. An ordinary “Not me, please ask …” referral failed. Message formatting was difficult to read. |
| Implement and finish | All three initial answers were signed and read. Claude implemented the option, tests and changelog. Finish was refused before the signatures. The saved advisory review reached the host and browser. | The review was not uniformly affirmative, and discovery missed documentation detail. Completion did not mean all oracle requirements were met. |
| Reuse | A controlled same-release question recalled the exact signed answer and its decision ID, reached the learned contact, and still required a signature. Another-release question remained an unapproved prediction. | The scope explanation confused source-code line ranges with scope names and reduced `1.26.x` to `release=1` in its prose. |
| Late human input | A member added a required follow-up through the UI. The real supervisor resumed the stopped Claude session. | The original completed task refused adoption. Claude opened a new task; its later owner answer did not wake the session bound to the original task. |
| Source changes | A material NET-101 edit removed authorization, marked the saved review stale and retained historical signed states. An old Slack approval was refused. Browser reapproval bound version 2. | This good behavior did not extend to a connector losing access to the source. |
| Observe and recover | Task history showed questions, respondents, referrals and signatures. External NET-101 lookup found four linked decisions; explicit work-item linking was idempotent. Restart interrupted a wait in 3.09 seconds and preserved state. Backup restored 66 tables to a separate database. | A supervisor status message was lost during a simulated Slack outage and could not be retried by replaying the report. |

## Findings

### H1 · P1 · Lost source access leaves authorization intact

With the root decision signed against NET-101, the fixture returned Jira HTTP 403 and made the imported Slack channel private. Both native sync calls correctly reported errors. However, `bridge_get_record` still returned the Jira source as `availability: available`, `current: true`, and the dependent decision remained `authorized: true`.

This is not a claim that the shared workspace leaked a newly imported private record: the initial restricted-record test passed. It is a failure to withdraw current evidence eligibility after access was explicitly lost. Previously imported bodies also remain readable under Raven's workspace-wide model.

`bridge/sources.py::_failed` only records the connection error. The Slack private-channel branch and Jira permission-error branch never produce an unavailable source observation. Treat explicit revocation separately from transient outages, invalidate affected premises, preserve historical snapshots, and require deliberate current-source review after recovery. Do not erase audit history or silently reauthorize on a successful poll.

Evidence: `source-permission-probe.json`, including before/after state and the exact sync errors. By contrast, `revision-probe.json` and `source-reapproved.json` show the content-change path working.

### H2 · P1 · A late required follow-up becomes an unresolvable blocker

The initial task `f07804519b5e` completed. While its host was finishing its report, the browser accepted a required zero-delay follow-up, `ce6a2d6a372c`. The tree now correctly showed one blocker, but `bridge_add_node(adopt=...)` refused: “This task is finished; start a new one with bridge_start_task.” `bridge_settle_node` could not settle the suggested node either.

The shipped supervisor successfully resumed the actual session. Claude tried the documented adoption flow, hit the refusal, and created `0b57d4a93abf` as a workaround. Its owner eventually signed through the browser. A subsequent supervisor poll returned “No new human or source events”: the session remained bound to the original task. The original blocker was still present.

The UI, completion state, adoption API and host binding need one lifecycle contract. Either atomically reopen the affected task and preserve its binding, or create and bind an explicit continuation while resolving the original follow-up relationship. Merely telling the model to make a new task is insufficient.

Evidence: `tree-postcompletion.json`, `tree-resume-new-task.json`, `host-mcp.jsonl`, and both supervisor result files. `canvas.add_followups` permits this state, while `canvas.add_node` rejects the completed run.

### H3 · P2 · Ordinary Slack answers overflow the full-source read-back

One of three initial decisions completed entirely in Slack. The other two answers, then the resumed host's zero-delay answer, were refused because the complete source review exceeded 12,000 bytes. The owner had to open an accountless page, enter the answer again and approve the source pins there.

The refusal is safe: Raven does not sign evidence it silently truncated. But three of four substantive replies needing the browser is far from the advertised low-friction Slack workflow. Full serialized decision snapshots include duplicated context, options, source state and signature metadata; this grows rapidly with related decisions.

Design a concise, lossless human review representation tied to exact stored revisions, with an explicit safe continuation for long source sets. Retain the complete audit snapshot separately. Do not fix this by silently clipping or removing the exact-revision requirement. Source review size is enforced in `bridge/source_review.py`.

Evidence: outbound replies in `slack.jsonl`. The computer-use recovery succeeded for all three cases.

### H4 · P2 · Natural referrals and ordinary identifiers are not natural to read

With a valid Slack timestamp, “Not me, please ask <@…> about compatibility.” returned “I do not know , please ask … in Raven.” The prefix bypasses inference in `slack_chat.respond`; the legacy parser then interprets the rest of the sentence as a person. Exact `not me <@…> just this one` worked, as did a differently phrased model-read referral with confirmation.

Outbound questions also contained literal `\u0060`, `\u005f` and `\u0028` strings. These are present in the actual Slack text and block payloads, not just JSON rendering. `approval_scope.transport_text` deliberately encodes punctuation across the whole message. Identifiers such as `retry_after_max` become hard to read. Provider-specific safe rendering should preserve ordinary technical text without enabling mentions or links.

Cards repeated context and options and often spanned thousands of characters. Useful summaries should lead with the decision, consequence, recommendation and uncertainty, with detail available on demand.

### H5 · P2 · Coverage and review categories still create avoidable work

The initial root proposal asserted an unbounded default, then listed substantially the same assertion under “The record does not establish.” It stayed partial and blocked, so no unsafe authorization followed, but the owner had to disentangle the proposal. The follow-up proposal similarly mixed supported facts and unsupported conclusions.

The actual patch passed 146 upstream retry/connection-pool tests. Independent checks passed all seven behavior tests plus a documentation-presence check. Two additional assertions against the **already preregistered** documentation requirements failed: the API text omitted zero-limit behavior and invalid-value behavior. Thus the complete independent score is **8/10**, not the earlier 8/8 subset.

The diff review completed in 44.1 seconds: one `follows`, one `departs`, one `unclear`. The departure described invalid-value validation as a condition absent from the signed answers, although it was stated in the pinned NET-101 policy. Evidence is not authorization, so this run does not establish that source text alone should authorize that policy. The useful distinction is “an additional policy choice was not explicitly approved,” rather than suggesting that rejecting an invalid constructor argument contradicts the signed rule about a server delay exceeding a valid cap. The `unclear` result for unchanged `increment()` is a legitimate limitation of reviewing only a patch.

Raven surfaced those review caveats; the host received them. It nevertheless described local work as “shipped” during its resume. Nothing was pushed upstream or deployed. Keep agent claims visibly distinct from verified actions.

### H6 · P2 · Supervisor Slack reports can be lost permanently

Restarting the app invalidated the local service double's shared network namespace. This was a harness outage, recorded and repaired by restarting the double. It also supplied a real delivery-failure test: the resume report attempted direct Slack posts while the service was unavailable.

The result was `reported: true`, `state: done`, `posted: 0`. After service recovery, replaying the exact report returned `duplicate: true` and posted nothing. `host_sessions.report` records the report before posting directly, outside the durable notification outbox. Put these messages in the same retryable outbox as decision notifications, with a per-report/per-thread idempotency key.

A failed inbound referral from the same outage was retained and successfully replayed through the operator API. That recovery path worked; supervisor reporting did not.

### H7 · P2 · Regression scripts do not currently give an all-green baseline

Eight of nine browser suites passed. `browser-agent-source-proposals.cjs:83` failed because an unscoped `.source-snapshot-body` locator matched two elements. The sign-off handler asynchronously refreshes and reopens a dialog, while the test waits for the HTTP response and can inspect a still-open earlier dialog. The failing log establishes a synchronization/selector problem to investigate; it is not proof that source authorization failed. No production or test fix was applied during this frozen run.

The deterministic walkthrough passed 36/37. Its exact expected-tool set omits `bridge_correct_node_facts`, `bridge_link_work_item` and `bridge_context_sources`. These extra tools explain that failure. This walkthrough still constructs an ownership map, so it is not the evidence for automatic cold start; the separate live loop is.

## Validation and practical recommendation

| Check | Result |
| --- | --- |
| Python suite, local Python 3.12 | 2,329 run; 117 skipped; no failures |
| Selected production-image/PostgreSQL contracts | 151 passed |
| Browser suites | 8 passed; 1 failed |
| DOM/UI logic suites | 8 passed |
| Deterministic OSS walkthrough | 36/37 |
| Independent upstream tests | 146 passed |
| Independent behavioral/documentation oracle | 8/10; both misses were documentation |
| Source revision invalidation and browser reapproval | Passed |
| Native source permission-loss invalidation | Failed |
| Required follow-up after completion | Failed lifecycle recovery |
| Restart and separate-database restore | Passed within the scope above |

The CLI receipts report $1.98 for the initial 66-turn host invocation and $3.55 for the 18-turn supervisor invocation. They do not measure all of Raven's own inference spending. The long initial session included waiting for simulated people; it is not a measure of pure implementation latency.

The browser showed useful attribution and readable task/history organization on desktop. Long questions, duplicated scope text and raw JSON remain cumbersome. The computer-use viewport override did not actually change the viewport, so mobile coverage here comes from the automated browser suites, not an asserted manual phone walkthrough.

The useful product foundation is present: an ordinary request starts a task; the agent discovers questions; records and human decisions share versioned links; humans can answer without accounts; signatures constrain completion; and later edits invalidate dependent approvals. **I would fix H1 and H2 before a customer relies on unattended continuation, and H3/H4 before promising that their team can stay in Slack.** Keep the pilot supervised and judge routing against a defensible set of knowledgeable contacts, not a future commit author. This run does not certify every integration, standing-rule shape, required-approver configuration, or the whole website promise.
