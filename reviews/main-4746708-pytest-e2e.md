# Real Claude Code loop on frozen pytest

October 12, 2026. Product revision during the coding loop:
`4746708bbd8c85f2d9d970520c24411f8401caf4`.

The loop completed. A real Claude Code host received an ordinary feature request,
registered it through the installed hook, investigated pytest and connected
records, asked two people, accepted a correction and required follow-up, resumed
the same session twice, implemented the signed decisions, read the asynchronous
diff review and saved its proof. The final patch passed **18/18 independent
acceptance checks** and **144 pytest JUnit tests, with 2 skipped**.

Two defects were fixed after the coding loop: the task overview hid required
follow-ups from its progress/headline, and a later cache hit falsely marked the
original task's proof stale. First-contact routing and conversation overhead
remain weaknesses. This is evidence for a supervised pilot, not effortless or
universally correct operation.

## Why this repository

The new repository was [pytest](https://github.com/pytest-dev/pytest), frozen at
`661682c25a9ff844a66ebd9b95d9925b8c4b65c8`, before October 12, 2025. In the
preceding year its non-bot, non-merge history contained **328 commits by 83
authors**. The largest author's share was **18.3%**; the top five accounted for
**60.1%**, and five authors had at least ten commits. These are name-based author
counts, not a verified roster of distinct employees. They provide substantially
more contributor variation than a single-author repository, without claiming
that public commit history reproduces a company's authority structure.

The sealed checkout contained 16,850 ancestral commits, no remotes, alternates or
future refs, and no unreachable objects. Raven indexed 2,000 commits and 635 files.
The host and server saw only sealed source mounts; the evaluator's policy oracle
and acceptance checks were outside both mounts. Host network access was limited
to Raven and Anthropic inference; an attempted GitHub request was refused.
Model training knowledge is outside this boundary.

This was a synthetic feature on real historical code, not a benchmark against a
held-out future pytest change. The oracle and test hashes were recorded before
Claude ran. Public repository identities were pseudonymized before routing or
inference. The two policy contacts, Lina Imani and Owen Vale, are fictional.
Their authority was stated in synthetic organizational records, not inferred
from who later committed a change. No public maintainer was contacted.

## Request and environment

The user prompt supplied no paths, owners or decision list:

> Our CI report uploads get huge when tests print or log a lot. Add an opt-in limit for captured output in JUnit XML reports, while keeping existing runs compatible. Make the behavior predictable for Unicode and failed tests, keep useful diagnostics, and document it for teams configuring CI. Please implement and test the change.

A fresh Docker/PostgreSQL 17 instance ran Raven with authentication and actual
inference. Claude Code 2.1.296 used its installed project hooks, HTTP MCP and
ToolSearch. Its initial process had a 70-turn budget and stopped at that bound;
the installed supervisor subsequently resumed the same session twice, using real
`claude --resume`. Neither resume received an evaluator-written implementation
prompt. A shortened stop-hold interval accelerated the wake test.

Slack, Jira and documentation servers were local HTTP simulations. The native
connectors imported the policy, a cancelled proposal and documentation. A Jira
security-level issue and a robots-disallowed document were excluded. Slack had
355 unique synthetic/pseudonymous contacts. No manual ownership map was entered.
The policy explicitly named Lina for behavior and Owen for documentation, but
left the new setting's name, units and truncation choice undecided.

## Observed trace

| Step | What actually happened |
| --- | --- |
| Hook and tool connection | The prompt hook registered task `1cfa3e5d9198` before repository work. Modern MCP discovery succeeded. |
| Investigation | Claude found the JUnit implementation and drafted a feature before requesting discovery review. Its initial name and head-plus-tail truncation were not the eventual approved design. |
| Context and discovery | Raven surfaced the policy and the cancelled NET-102 proposal. Cold retrieval and inference required several waits. Review identified missing behavior and documentation judgment; the first finish attempt was refused. A malformed review retry without observations was also refused, then corrected by the host. |
| First contact | The behavior question first reached a historical contributor instead of Lina. The documentation question reached Lina instead of Owen. **0/2 first deliveries matched the explicit fixture contacts.** Both recovered through one confirmed human handoff. |
| Non-answer | “I haven't reviewed this yet; I'll look tomorrow” produced no answer or authorization. |
| Correction | Lina required `junit_output_limit`, default zero/unlimited, prefix-only Unicode truncation per raw stream, a fixed marker, preserved diagnostics/filtering and invalid-setting rejection. Raven read the complete answer back and required an exact confirmation. |
| Required follow-up | Lina required Owen's documentation review. The confirmed original request survived interpretation and adoption into a second decision. It blocked completion until handled. |
| Wakeup | The installed supervisor resumed the original Claude session after each owner's reply. Both resumes exited successfully, acknowledged the events and posted progress back into the task's Slack threads. |
| Implementation | Claude changed the initial draft to match Lina's answer, then documented Owen's example and the distinction between a per-section character limit and total XML file size. It ran tests and phase-specific output experiments itself. |
| Final discovery | Raven returned two factual investigation items concerning phase accumulation and the version marker. Claude investigated and recorded dispositions without turning them into unnecessary human decisions. |
| Diff review | The first reading took **140.2 seconds** and returned behavior `unclear`: its counterexample pass did not complete. Docs read `follows`. The host received this, retried, and received a complete second reading after **121.1 seconds**, with both decisions `follows`. Neither is a substitute for tests. |
| Finish proof | Claude refreshed the proof after the terminal review. It had two authorized answers, one adopted follow-up placeholder, zero blockers, `review_pending=false` and `review_snapshot_current=true`. |
| Optional browser | The actual accountless recipient page showed the original request, both signed answers, people contacted, referrals and history. Its required-follow-up progress bug is described below. No recipient account was needed. |
| Reuse probe | A separate evaluator-driven MCP task reused Lina's exact answer and routed to Lina. It remained `authorized=false`, `blocking=true`, `signoff=required`. Its finish was refused at the discovery prerequisite; this probe does not independently demonstrate the later sign-off refusal. The probe was then abandoned. |

The private tests were never shown to Claude. Both the baseline and initial draft
passed 9/18 acceptance checks; after the human correction the implementation
passed 18/18. The final diff submitted by the host is exactly 14,350 bytes, SHA-256
`bb20337feefdc3e3e5e63de8d73ce0f08e37deef1278126b6866dc5b4a8276ed`.
Its file sections match the worktree diff. The host appended the new changelog
last, so canonical git file ordering produces different bytes; that is recorded
separately rather than called a mismatch or omitted.

## Fixes and validation

**Required follow-ups were invisible in the overview's completion summary.**
Immediately after Lina's required follow-up, her page still said “Nothing on this
task waits on anyone now.” Backend progress and the browser headline both
excluded every suggested node. Required suggestions now count as blockers and
say that the coding agent must adopt them. Optional suggestions remain optional.
Unassigned decisions also prevent an all-clear headline. The copy describes
recorded decisions rather than claiming Raven has discovered every decision.

The backend regression fails on the old code. A DOM regression exercises the
shipped rendering functions for required, optional and unassigned cases. The
final live browser page was checked after applying the fix and restarting; the
pending required state itself was regression-tested, since the real host had
already adopted it.

**A later cache hit falsely invalidated the source task's proof.** The proof
comparison included both directions of a derivation link. A new consumer of
Lina's answer therefore made her original proof read stale, despite no change
to its answers, signatures or premises. The comparison now ignores explicitly
identified downstream consumers only. Incoming or ambiguous derivations and
source changes still count. Saved bundles are never rewritten. A regression
reproduces real reuse and fails before the fix; another verifies that a new
incoming source still invalidates the proof. On the live database, the original
bundle is byte-for-byte unchanged and current again after reuse and restart.

Validation of these changes:

* 76 briefing tests passed, including the new required-follow-up case.
* 27 proof tests passed, including stale-answer, incoming-derivation, restart,
  exact-diff, asynchronous-review and failed-review behavior.
* 12 focused PostgreSQL contracts passed in isolated schemas.
* Both affected DOM suites passed; the existing source-review suite covers 31
  cases. Ruff passed for changed Python files.
* The generated pytest change passed 144 upstream JUnit tests (2 skipped) and
  all 18 independent acceptance checks. These are separate from Raven's tests.

The full Raven suite and Docker image rebuild were not repeated for these small
changes. The updated Python and JavaScript were copied into the isolated running
image for the live persistence and browser checks. The main coding loop used the
unmodified product revision named above.

## Remaining weaknesses

**Routing still makes people do avoidable work.** Explicit contacts in the policy
did not win first delivery. The host also omitted structured paths when adding
nodes, despite knowing them, and prematurely described Owen as unavailable.
The original node still displays `path: unknown`. This is a combined host and
product integration weakness, not proof that history ranking alone failed.
Recovery and subsequent reuse worked, but two handoffs were still necessary.

**The Slack exchange is too heavy.** The core loop generated 23 outbound messages,
including readbacks, confirmations and supervisor updates. Technical identifiers
and punctuation appear as literal Unicode escapes in some messages. Full scope
and repeated options make the review hard to scan. Supervisor updates also go to
earlier recipients who already handed the question on. Completeness is preserved,
but the interaction is not yet as lightweight as the product vision.

**Discovery and review can be slow.** The agent drafted first, then discovery
caught missing judgment before finish. That allowed preliminary work but caused
rework. The asynchronous review receipt kept the host connected; it did not make
the underlying cold retrieval or model work fast. One conformance attempt failed
conservatively and needed a retry. Both readings remain in the evidence.

Harness issues were kept separate: duplicate email spellings initially broke
the simulated directory response, and the frozen checkout needed an ancestral
version tag for pytest's own minimum-version check. Both were repaired before
the coding run. No corresponding Raven source change was made.

This run does not certify a real Slack installation, OAuth, tenant permissions,
rate limits, GitHub webhooks, a cloud MCP Events subscriber or every coding host.
It does demonstrate the installed local hook/supervisor loop with real Claude,
inference, code, tests and PostgreSQL. No patch was sent to upstream pytest.

[Redacted evidence and reproduction notes](../evals/newdev/results/pytest-loop-2026-10-12/README.md).
