# Experimental contact discovery: validation status

**Known-failing archival snapshot. Do not merge, deploy, or enable
`BRIDGE_CONTACT_CLAIMS=1` outside isolated validation. Use `main` for the current
validated implementation.**

This branch preserves the source of candidate
`629a8698be8fb8a1e717b2a38915ad9bfbc0286f` (source tree
`fe77c1ee0bfa86cad609975368688d4b66d85a14`) on public baseline
`cb4c6c2b0f48ac0d1d10e372745da16baa742d83`. Publication adds status documentation;
it does not repair the experiment or claim readiness.

## Recorded checks

The frozen candidate passed 45 new tests, a 1,283-test native aggregate,
190 real PostgreSQL tests, four actual browser suites and two DOM suites.
These passes do not override the independently reproduced failures below.
They are evidence for this historical source, not for a port onto newer `main`.

## Known blockers

1. Queued contact invitations can retain an old Slack destination after a
   directory change.
2. Contact delivery and pending claim confirmation do not consistently respect
   current Slack unavailability.
3. The old baseline loses stable person identity through a display-name
   round-trip, which can assign a different same-name person. That defect was
   reproduced separately on public code and repaired in `main` by
   `ffc9d69a33d8622520d07b6f7b913255fcae20e7`; this archival snapshot does not
   contain or validate that later correction.

The design document describes intended boundaries, not guarantees that all
those boundaries currently hold.

## Old-base warning

This is not current `main` plus an isolated feature patch. It also predates
cached-review input binding (`f09a0ca`) and task-resume repository checks
(`06b445e`), among other later fixes. The earlier first-contact wording change
is already represented on `main` as `389a77a`; do not apply it twice in a future
port.

## Validation limits and next steps

Contract tests use scripted extraction outputs and fake Slack transport. They
do not establish live-provider role-extraction accuracy, wrong-contact rates,
or robustness across representative natural-language evidence. The feature is
opt-in and limited to its documented canvas/Slack flow. No production deployment
or real customer approval is represented.

Before a readiness claim, correct the reproduced failures, review a port onto
current `main`, and rerun exact-revision identity, availability, stale-delivery,
concurrency, PostgreSQL and runtime UX checks. A separately authorized bounded
provider evaluation is still needed before claiming real extraction accuracy.

## Publication checks (2026-10-06)

The exported source, with these documentation warnings, passed a fresh native
aggregate of **1,283 tests, zero failures or skips**, in 171.345 seconds. Provider
credentials were unset for this local test process. The independent failing
invitation/availability reproductions are separate evidence and remain unresolved.
