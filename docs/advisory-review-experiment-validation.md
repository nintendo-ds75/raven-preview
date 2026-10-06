# Experimental advisory review: validation status

**Unproven archival experiment. Do not merge or deploy this branch as a reliable
semantic reviewer. Use `main` for the current validated implementation.**

This branch preserves candidate `34ba775b1b1fbd2bef29fe6f240b9cb5f5a6c4a1`
(source tree `451f533d4fe4b86f11cd6b9eceab7a08d60ce557`) on public baseline
`e7d8b6379300b6ad7482538a57bcb5d95f3c8513`. Publication adds status documentation;
it does not establish a successful semantic fix.

## Preserved work

The snapshot includes bounded effort controls, grounded behavioral allegations,
separate requirement-coverage accounting, server-owned approved-source bindings,
and historical-review notices. Earlier effort, behavioral, coverage and notice
work is incorporated in this snapshot rather than presented as separate fixes
ready for `main`.

## What the evidence does and does not show

Earlier live experiments were inconclusive or failed semantic controls. An
apparent machine-positive result was rejected because it preferred an
implementation mechanism without a grounded behavioral witness. A later coverage
experiment accepted a faulty implementation after the first reading narrowed
an approved requirement. Those outcomes remain failures or inconclusive results;
they have not been relabeled as successful detection.

The latest approved-source-v3 changes aim to retain the exact approved source
through both review stages, but have not been validated with a real model.
Deterministic tests cover protocol, parser, source-binding, persistence and
rendering behavior. Such tests do not establish semantic detection accuracy,
false-positive rates, completeness, or production readiness. A full review may
still honestly end inconclusive.

Prior PostgreSQL/browser results for other review versions must not be treated
as a complete runtime pass for this latest source-bound candidate. No live
provider call or deployment is part of this branch-publication step.

## Historical-base warning

This is not current `main` plus a feature patch. It predates later fixes including
fresh asynchronous finish guidance (`cb4c6c2`), signed-question review input
fingerprints (`f09a0ca`), repository-resume checks (`06b445e`), and stable-person
referrals (`ffc9d69`). Do not use a whole-tree replacement to integrate this
snapshot into `main`.

Before any readiness claim, review a port onto current `main`, rerun exact-revision
native/PostgreSQL/browser checks, and perform separately authorized bounded live
semantic controls with independently adjudicated behavioral witnesses. Preserve
failed outcomes and inconclusive stages rather than weakening their gates.

## Publication checks (2026-10-06)

The exported source, with these documentation warnings, passed a fresh native
aggregate of **1,316 tests, zero failures or skips**, in 166.444 seconds. Provider
credentials were unset for this local test process. This is deterministic
regression evidence, not a successful live semantic examination.
