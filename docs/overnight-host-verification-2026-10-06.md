# Overnight host examination — 2026-10-06

**The target of roughly 100 end-to-end repetitions was not reached.** The broader
breadth cohort contains **39 genuine cold-start coding-host attempts**, covering
**37 distinct scenarios across 21 public open-source repositories**. There were
**16 first-attempt full passes under each run's frozen, recorded rubric**.
Production fixes were committed incrementally to `main`; nothing was deployed.

This examination extends the [earlier verification](verification-2026-10-06.md)
and [acceptance matrix](acceptance-2026-10-06.md). It does not establish complete
model correctness or production readiness.

## What actually ran

The hosts inspected and edited real public codebases, called Raven through MCP,
ran repository-native tests and submitted changes for Raven's approval/proof
workflow. These were actual Anthropic responses, not replaced model outputs:

| Cohort | Genuine cold starts | Full recorded-rubric passes |
|---|---:|---:|
| Official Claude Code CLI | 29 | 12 |
| Custom Anthropic Messages API coding hosts | 10 | 4 |
| Total | 39 | 16 |

The continuous two-lane campaign accounts for 37 attempts: 16 full passes,
9 host-completed runs that failed one or more gates, and 12 partial runs. Two
earlier Messages API cold pilots of the same scenario account for the other
two attempts; both were partial. They do not add distinct scenarios. The custom
Messages hosts disabled thinking and used versioned caching/output limits; they
are not equivalent to the official CLI or its default host configuration.

All 16 full passes had byte-identical submitted and independently captured
patches. Separately, one original byte-mismatch failure was adjudicated by
applying both unchanged patches to the pinned baseline: they produced the same
Git tree. One earlier partial API attempt later succeeded through a composite
recovery using two continuation segments. Neither result changes the original
scores or adds cold-start credit. Two Bidict continuation segments remained
partial, making four continuation segments in total.

Seven provider-rejected starts with no model response,
one pre-provider isolation failure, and three preparation failures are excluded.
Seven fresh history/reuse CLI sessions, a two-session complex multi-repository
attempt/recovery, and two initial workflow pilots are separate supplements.
Four early breadth attempts used deep-inference-off ablations; the other 35 used
core deep inference, with recorded source and transport differences.

The full-pass rubric included the available native/held-out tests, current
approval state, completed task, bound patch and terminal advisory-review state.
A terminal `unclear` review could satisfy the completion gate; the Pathspec pass
included that outcome. A recorded full pass does not mean every behavior was
proven correct or every advisory review approved the code. Raven binds the diff
submitted by its host; independently checking that it matches the workspace was
an additional evaluator check, not a product guarantee about unseen files.

## Workflow findings

- Static source retrieval, signed decisions, scoped referrals, temporary cover,
  corrections, readbacks, required follow-ups and completion gates were exercised.
  Correctly retrieved context sometimes still required human sign-off.
- Fresh hosts retrieved decision history and matching explicit standing rules
  without new notifications. Scope changes and narrowed rules blocked reuse of
  authority. Historical answers remained evidence rather than new permission.
- Initial stakeholder selection and question quality were mixed. Some contacts
  required referral; some hosts created overlapping child questions and repeated
  interruptions. Backup authorship could outrank an explicit primary-owner claim
  written only in source prose. The wording fix describes that result as a first
  contact; it does not fix ownership extraction.
- Hosts sometimes misstated returned history, including confusing an immutable
  saved review with the later state of the same review, or describing a withdrawn
  decision as still waiting. Citations alone did not guarantee grounded summaries.
  There is no defensible overall numeric question-quality score.
- The complex two-repository workflow never fully completed. Its recovery passed
  native suites and the original 31 held-out checks, but approvals and proof
  remained unfinished; subsequent versioned checks found two integer-type
  validation violations. That recovery is not counted as a full success.

## Production fixes published during the examination

The final implementation checkpoint is
[`ffc9d69a33d8622520d07b6f7b913255fcae20e7`](https://github.com/nintendo-ds75/raven-preview/commit/ffc9d69a33d8622520d07b6f7b913255fcae20e7),
Git tree `e73d5b489c24f3e8f2968419aabcc13d24ec645d`. Changes include:

- Explicit retry/recovery for failed advisory review, bounded output and honest
  incomplete states; fresh finish guidance after asynchronous review waits.
- Exact submitted-diff checks, proof persistence across client disconnects, and
  explicit separation of immutable saved review snapshots from current review.
- Read receipts for the human revisions actually shown, completion-check parity
  for agent REST paths, and transitive invalidation after rule or source changes.
- Preservation of task-only approval qualifiers, answer-correction versus
  question-reframe boundaries, fresh confirmation after invalidation, and shared
  bounded logical repair for malformed conversation actions.
- Notification deduplication by invalidation epoch and recovery of substantive
  new answers after stale readbacks, while stale short confirmations remain invalid.
- Cached-review identity bound to signed questions and answers, plus clearer
  parent/context guidance before suggesting additional questions.
- Rejection of a different repository on task-ID resume, with actionable
  instructions to abandon the mistaken task and start a new correctly scoped one.
  This addresses a real loop that left hosts querying the wrong repository.
- Stable person identity through assignment, referral, delivery and failed-send
  retry, preventing namesakes from acquiring another person's owner binding.
  Full validation caught a Teams override regression in the candidate; it was
  corrected before publication and the complete checks were restarted.
- Finite wait validation, consistent malformed MCP argument errors, transparent
  evaluator timeout forwarding, and corrected test synchronization/portability.

Successful live breadth runs used revisions between `2d210c4` and `cb4c6c2`.
The latest fixes were validated deterministically and in the runtime below;
they were not all replayed with a live coding host after inference became unavailable.

## Final runtime verification

All checks below refer to the exact implementation tree above; publication
metadata differs from local validation commits, but tree equality was checked.

- Native aggregate: **1,267 tests, zero failures or skips**, 180.478 seconds.
- Focused real PostgreSQL run for the final identity correction: **262 tests, zero
  failures or skips**, including all 14 identity and three new Teams cases.
- Final normal-production-image aggregate with real PostgreSQL and both optional
  Teams/Agents dependencies: **2,049 tests, zero failures, 13 packaging skips**,
  571.909 seconds. All 13 skipped source-checkout cases passed separately in
  0.435 seconds, with zero skips. Container skips are not counted as passes.
- All four actual Chromium suites and both DOM suites passed. The browser runner
  used Playwright 1.63.0, Google Chrome for Testing 153.0.8010.12 and Node
  24.20.0.
- Exact-SHA [GitHub CI](https://github.com/nintendo-ds75/raven-preview/actions/runs/37490382443)
  passed. This workflow checks commit conventions; the backend/browser runs above
  are separate evidence.

Earlier, a fresh README-exact `./setup --demo` installation with new Docker
volumes and no provider configured completed synthetic local onboarding and
showed five seeded tasks with no JavaScript errors. This was an installation/UI
check, not live model execution. Repeatable backend/browser commands remain in
the [earlier verification report](verification-2026-10-06.md#repeatable-commands-and-boundaries).

## Remaining failures and limits

The original Cachetools patch passed its registered tests but failed a separate
iterator/reinsertion counterexample; Raven's advisory review missed it. Another
saved review accepted a truthiness check where the approved contract required
validating a nonempty string, including before an early return. A separate host
implementation confused explicit `None` with an absent value; that run never
reached terminal review. These are distinct from authorization or transport tests.

Focused reviewer experiments did not establish a reliable semantic fix. One
machine-positive result was rejected because it demanded a preferred mechanism
rather than demonstrating a permitted behavioral counterexample. Later controls
also failed. Stronger source-bound review and contact-only ownership-discovery
candidates remain excluded from `main`. Their historical snapshots are now
preserved on clearly labeled [review experiment](https://github.com/nintendo-ds75/raven-preview/tree/experimental/source-bound-review-2026-10-06)
and [contact-discovery experiment](https://github.com/nintendo-ds75/raven-preview/tree/experimental/source-contact-claims-2026-10-06)
branches with validation-status documents. These older-base archives are not
merge-ready or deployable. Independent review found additional contact-invitation
boundary issues despite passing regression suites; those need further correction
and validation.

Slack people, conversations and approvals were synthetic. This does not certify
live Slack/Teams/GitHub installations, real employee authority, physical voice,
enterprise isolation, upstream acceptance or deployment. Public OSS tests were
scoped to each scenario; passing those suites does not imply exhaustive coverage.
Raven still depends on the host to call its tools, read corrections and implement
the approved behavior. Read acknowledgment is task-scoped, without independent
per-agent/session awareness guarantees. Existing stores with historically
ambiguous owner bindings need explicit operator review; the identity fix does
not guess how to reassign those records. Ambiguous-name routing learning can
still abstain. Advisory review remains fallible.

The campaign stopped after a provider-side inference availability failure was
verified. No further successful inference occurred after that failure; the seven
provider-rejected, zero-response starts remain recorded and excluded from
attempt counts. The unfinished live replays remain unverified.
