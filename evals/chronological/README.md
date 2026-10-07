# Strict chronological supplements

Use Raven's existing [evals/audit engine](../audit/README.md) for fixture freezing, rubric pinning, observable call capture, append-only ledger, scoring and the interactive viewer. This directory adds strict historical boundary checks and context-admission receipts; it does not introduce a second runner, ledger, renderer or scoring format.

The [coordination acceptance matrix](../../docs/chronological-coordination-evaluation.md) specifies stakeholder/question/cache outcomes and separates actual integrations from simulations. No selected task, expected stakeholder set, reference patch, future author, private result or credential is included here.

## Offline validation

From the repository root:

```sh
python -m unittest evals.chronological.test_chronological -v
```

Tests create temporary synthetic repositories and call the existing fixture freezer. They cover physical absence of future Git objects, strict exclusive cutoff selection, author-date skew, unexpected refs/promisor routes, and conservative context filtering. No provider account is used.

## Compose with the existing freezer

The trusted evaluator selects its source repository's intended main branch. Call `boundary.select_cutoff(repo, cutoff)` to choose the first first-parent ancestor strictly before the declared boundary. Freeze that commit using `evals.audit.fixture.freeze`, including an independently reviewed exact brief and provenance. Then call `boundary.verify_chronological_fixture(destination)` before copying the allowed checkout and admitted records into the host environment.

The supplementary policy is explicitly stricter than the general freezer: both author and committer times must be strictly before the cutoff, refs must be absent, and submodules/promisor routes are rejected. The existing engine's exact physical-object-set and clean-tree checks remain in force. Unknown or clock-skewed inputs require adjudication; dates do not cryptographically prove when an object first became public.

Run Git checks only on trusted evaluator-owned fresh exports before host edits. After launch, repository operations must use that host's unprivileged identity and credential-free environment. Before launch, verify actual UID readability, all host tools, network/provider routes and the corpus Raven ingests. Filesystem separation or an old HEAD alone does not provide full isolation.

## Context admission contract

`filter_context.admit` is a metadata predicate, not a connector or authenticity verifier. A trusted collector must verify the actual body against an admitted Git object or independently archived snapshot; an asserted hash alongside arbitrary text is insufficient.

Mutable issue/comment bodies need their own revision timestamps. Immutable events need a receipt already in a separately verified allowlist and a pre-cutoff occurrence time. Reviews against future commits are excluded. The predicate does not fabricate historical labels, assignees, participants, resolution or unreadable old bodies from current API responses.

The `provider_access` field is normalized evidence supplied by that trusted collector. A synthetic `authorized: true` fixture is not a live-provider compatibility result. Actual missing or null ACL data is rejected. This helper's accepted output still needs the full `evals.audit.fixture.validate_records` contract before incorporation into a sealed fixture; neither predicate alone proves authenticity.

## Public code and private gold

Keep actual reference code, full future repositories, expected people, private rubric labels and run results outside every host-readable directory, installed source tree, tool response and public repository. A hidden file on another public branch can still be fetched. The live host must have no permitted route to future public source/search content during the held-out task.

Only cutoff identity and boundary receipts belong in a host-visible manifest. A reference implementation is one acceptable solution, not a mandated patch or stakeholder oracle. The task describes a contemporaneous need without naming the future answer or author.

The existing audit engine records model-owned calls and evaluator interventions separately. Preserve original questions before later human answers or reframes. Its timeline must display recorded graph state, not infer learning from tool success. Render and review any artifact before sharing, and retain an independent final digest.

These utilities do not change credentials, OS/network permissions or live approval rules. Their offline results do not certify official host hook loading, continuation, real connector ACLs, stakeholder accuracy or warm-cache authorization. Cutoff isolation prevents test-time retrieval leakage, not possible pretraining exposure.
