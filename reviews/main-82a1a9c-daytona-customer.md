# Browser customer loop in Daytona, 13 October 2026

The complete customer stack ran in a disposable Daytona machine: Docker Engine,
PostgreSQL 17, Raven, the real Claude Agent SDK, and isolated command containers.
The browser task reached **Patch ready for manual review**. Five decisions were
signed in the web inbox. The final patch passed the project's 332 tests plus
seven independent contract checks. The automatic advisory diff review did not
complete; this is not a clean automated-review pass.

Base Raven commit: `82a1a9c3df05d95f69dfef5509a7300e5d242776`. The implementation
and evidence are committed together after that baseline. Tested source hashes
and the final customer image ID are in the evidence directory. The private
architecture brief, credentials, provider session files and database backups
are not included.

[Evidence, patch and independent checks](../evals/newdev/results/daytona-customer-2026-10-13/).

## What was real

* Daytona's `docker:28.3.3-dind` machine, 2 CPUs, 4 GiB memory; Docker 28.3.3,
  Compose 2.39.2. Fresh Compose volumes, then in-place image upgrades.
* Raven account creation, repository selection, task entry, corrections and
  sign-off, resume, overview, final result and question-only recall in the browser.
* Claude Agent SDK 0.2.165 with `claude-sonnet-4-6`. Raven's separate internal
  inference used Sonnet 4.6 and Haiku 4.5. The host used Raven's tools, not an
  external user terminal. No file paths or prewritten decision list were supplied.
* `pallets/itsdangerous` at `672971d66a2ef9f85151e53283113f33d642dabd`, dated
  14 June 2025. The coding workspace contained that committed snapshot only.
* A real application-container restart while waiting, real no-network test
  execution, a PostgreSQL dump/restore, and task-file archive/restore.

The owner and rollout policy were synthetic. Mira Security was explicitly given
trial authority through the UI. The policy was imported as a fixture. No Slack,
Jira or GitHub installation was exercised in this loop. The clone was **depth 1**:
only one author appeared in its ingested history. This run therefore does **not**
validate production-like contributor distribution, cold-start routing, held-out
owner accuracy or a year-separated historical benchmark. Public code can also
be known to a pretrained model. Those limitations must remain in the denominator
of the larger acceptance plan.

## The customer sequence

| Step | Observed result | Remaining friction |
| --- | --- | --- |
| Install Docker stack | Fresh PostgreSQL and Raven started after fixing the checkout path mismatch. | This was an operator-assisted Compose installation, not a measured unassisted onboarding study. Test dependencies still need a prepared command image. |
| Start from plain-language task | The browser created one task and one real Claude session. The host inspected code and policy and raised four decisions, then a fallback-signer decision. | Several questions were leading or overlapping. The initial proposal claimed no new attack surface without evidence. |
| Wait for judgment | Four unsigned decisions blocked completion. Raven persisted a waiting state even when Claude ended its turn without calling the wait tool. | A separate earlier hybrid probe measured 68.6 seconds with no new host turns or internal calls. That measurement is labelled separately; it was not the full Docker deployment. |
| Restart and reply | Restart preserved task, session and accumulated cost. A UI correction woke the same Claude session. Five answers were signed, including two explicit independent owner judgments where source provenance could not support ordinary reapproval. | Source-review controls were more involved than a simple approval. This is not proof that every source-revalidation path is smooth. |
| Implement | The host first wrote its old sentinel proposal while newer owner answers arrived. Finish discovery caught the mismatch; the host changed code, tests and docs to the signed None-falls-through behavior. | Work done before an answer can need substantial revision. The UI still shows the original misleading question titles beside later corrected answers. |
| Test | 332 upstream/generated tests passed. Seven separate checks covered both serializers, inclusive boundaries, future timestamps, explicit None, numeric overrides, zero/negative defaults, unsafe loads and a genuine fallback signer. Final combined run: **339 passed**, exit 0. | Passing this suite does not establish general security or correctness outside those requirements. |
| Finish | Repeated advisory review failures paused the task. An explicit browser instruction selected manual review. Raven retained a permanent warning and presented the downloadable patch, with five approvals and zero waiting. | Review churn is the main unresolved usability problem. The automatic review was inconclusive, not passed. The host's final security summary also compressed an approval into overly strong wording. |
| Recall who and why | Before fixes, the read-only answer lost attribution; then a whitespace-only citation mismatch rejected a reading. After fixes, the exact same browser question correctly named Mira, explained None-falls-through and its rationale, and left TimestampSigner unchanged. | One final live success is not a reliability rate. Model support checking can still return an honest refusal. |
| Restore | All **80** table counts matched; task, host and decision rows matched exactly. Restored task files produced the identical patch SHA-256. | A same-version restore was tested, not every historical migration or a different cloud provider. |

The final database still contained one browser task, one host binding and five
decisions after the question-only reads. Those reads created neither a coding
session nor new authorization. The host reported approximately **$3.075** for its
session; this excludes Raven's internal inference and is not total test cost.

## Fixes made during the run

1. Compose ingestion and browser execution now use the same checkout beneath
   `/repos`. Previously the app could enter a restart loop after ingestion.
2. A host ending its turn with pending decisions now leaves a durable wait that
   wakes on a decision change. Executor unavailability also pauses instead of
   sending the agent through repeated failing commands.
3. Claude SDK cost receipts are cumulative within a resumed session. Summing
   them falsely exhausted the $5 task budget. Raven now reconciles durable
   receipts by session, including recovery of an already inflated aggregate.
4. Question-only evidence now includes the current answer, actor, signer,
   rationale, revision, scope and review state. It does not treat the original
   question's assumptions as an approved API contract.
5. Citations accept whitespace wrapping but still require the same source words.
   Adding a negation remains rejected. The support check remains required.
6. The task UI no longer displays a host-turn success label that could be confused
   with success of the whole task. Incomplete review remains visibly incomplete.

The broader implementation adds real host adapters, explicit provider settings,
durable tool intents and operator reconciliation, a read-only question entry,
source access leases and withholding of affected derived context. Tests cover
these mechanisms separately. The Codex adapter is experimental and has no live
acceptance result in this run.

## Verification and reproduction

* Full offline suite before the final three focused regressions: **2,451 tests,
  four skipped**. That total was not rerun after the small final fixes.
* Focused PostgreSQL run: **135 passed**. Final inquiry/host regressions:
  **21 passed** locally; final customer image inquiry tests: **8 passed**.
* Eight browser DOM/UI logic suites passed. Actual browser interactions and
  screenshots are separate evidence, not inferred from those suites.
* Ruff and `git diff --check` passed.
* Actual command container: UID 65534, no provider credential variables, no
  Docker socket or server-data path, read-only root, network unavailable.

To reproduce the generated-code checks on an isolated checkout with pytest:

```sh
git checkout 672971d66a2ef9f85151e53283113f33d642dabd
git apply /path/to/evidence/customer.patch
cp /path/to/evidence/independent_contract.py test_independent_contract.py
PYTHONPATH=src python -m pytest tests test_independent_contract.py -q
```

Expected final patch SHA-256:
`9fff8d739d652d741f8d9638f5728a01b5673216e18d9ddf151bb14e45aaf4ee`.
The test result and restored-file result bind to that hash.

## Not yet a release certificate

The 100 predeclared adversarial trials, paired quality experiments, real human
study, live OpenAI host, real connector installation and production-like routing
benchmark remain unverified. Native source access checks establish workspace
access, not per-user ACLs. Internal inference has no durable total dollar budget;
Codex has no priced dollar budget. Throughput remains one host turn at a time.
Atomic outbound delivery/access checks and atomic file writes need further work.

This is evidence that a supervised browser customer can get a real approved
patch out of Raven on a separate cloud computer. It does not establish that the
entire architecture brief is finished or that the experience is yet effortless.

The two disposable Daytona sandboxes were deleted after export and the temporary
trial API key was revoked. Existing user sandboxes and the pre-existing key were
left unchanged. The source and evidence scan found no live credentials; its only
private-key-pattern match was the existing literal `test` PEM fixture. Fixture
privacy checks passed (10 tests).
