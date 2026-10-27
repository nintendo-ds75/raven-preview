# Independent artifact verification

Raven's existing host-reported proof remains host-reported. This optional verifier
adds a separate observation: **operator-selected checks, actually executed by
Raven's configured no-network sandbox against the candidate's bytes**. It does
not replace the existing finish gate or human judgment and never grants shipping
authority.

## Configure

Set `RAVEN_VERIFICATION_PROFILES` to an absolute JSON file controlled by the
operator, **outside Raven's private task copies**. The API selects a profile ID;
it cannot submit commands, success booleans, fixture paths or expected results.
The existing Workbench sandbox must be enabled. No sandbox or profiles means
unavailable, never a passing badge.

```json
[
  {
    "id": "billing-boundaries",
    "label": "Billing boundary examples",
    "repository": "acme/billing",
    "commands": ["python -m unittest discover -s trusted_checks"],
    "overlay_dir": "/srv/raven-verification/billing",
    "coverage": "Operator-owned positive, negative and boundary examples for the approved billing behavior; not performance or deployment verification."
  }
]
```

The optional `overlay_dir` contains trusted test inputs copied over the disposable
verification copy. For example `/srv/raven-verification/billing/trusted_checks/`
contains the test modules invoked above. Neither the configuration nor the source
fixtures are supplied to the coding harness. Symlinks, oversized inputs and
profiles for another repository are refused. Keep these paths outside any other
location writable by the coding harness; the task-copy check is not a substitute
for filesystem permissions.

The overlay is **not a read-only mount inside the test container**: the current
sandbox copies inputs. Raven compares every original input after each command;
changing or deleting candidate inputs or trusted fixtures invalidates the result.
Generated output files may be added. Sandbox output is applied only to the
throwaway copy, never the delivered candidate.

## What a receipt proves

A receipt binds:

- The actual source base revision, complete patch digest and bounded candidate
  input-tree digest (same files/modes the existing sandbox accepts, excluding
  `.git`, dependency and cache directories).
- The current core decision revision vector, including signed scope/answer
  content. Unresolved judgment or stale source access prevents verification.
- The exact operator profile and trusted fixture content digests.
- Actual command exit status, timeout flag and captured output.

Candidate, task state, decision scope and profile are checked again after the
commands. Changed inputs produce `stale`; test mutations produce `invalidated`;
nonzero or timed-out checks produce `failed`; unavailable sandbox execution
produces `unavailable`. Only a successful bound observation produces `passed`.
Receipt rows are append-only. On later reads, `current` and `usable_pass` are
computed afresh; a historical passed result does not silently apply to new bytes.
Source revocation withholds receipts and copied output.

The receipt uses `assurance: independent_profile`. With trusted fixtures,
`coverage_basis` is `operator-held-fixtures`. Without them it is
`repository-owned-tests-or-commands`: independently running `npm test` does **not**
make agent-edited tests independently authored behavioral coverage. Existing
host-reported checks and ordinary Raven-observed worker commands are distinct
assurance levels; neither is upgraded by this feature.

## Integration contract

`artifact_verification.migrate(db)` adds one portable receipt table.
`Verifier(workbench).config()` reports actual profile/sandbox readiness.
`verify(task_id, profile_id, identity)` requires a completed idle task and a human
member; server routes must apply normal authentication and CSRF controls.
`list(task_id)` requires current task source visibility and recomputes usability.
This module does not mark a business objective achieved or change task approval.

## Verification performed

Contract tests use real git checkouts and Raven's actual SQLite graph, with an
explicit fake sandbox. They exercise byte binding, stale receipts, source and
trusted-fixture mutations, profile drift, failed commands, missing sandbox,
agent refusal, repository scoping and malformed sandbox responses. A Docker
executable is not available in this implementation environment, so **real Docker
execution of this new verifier has not been exercised here**; the existing
sandbox implementation is reused, not substituted with a host shell.

Passing these checks is bounded evidence, not a proof against malicious tests,
a universal semantic verifier, a customer-outcome measurement or permission to
publish. The configured coverage statement and gaps stay in every receipt.

## Actual Docker acceptance

The fake-sandbox unit tests do not establish container isolation. The explicit
`docker-verification` CI job builds the optional runtime image, runs installed
harness initialization/scripted loops with networking disabled, and runs the
independent verifier against real Docker containers. Its held-out check observes
behavior, unprivileged UID, only a loopback network interface, no host-only key,
no Docker socket and a read-only root. Separate checks require failure on a
nonzero exit and invalidation when a check mutates its input.

Run it locally only with an available Docker daemon and pre-pulled test image:

```sh
docker pull python:3.13-slim-bookworm
BRIDGE_TEST_DOCKER=1 python -m unittest discover -s tests -p test_artifact_docker.py -v
```

These opt-in tests were added but **not run in the implementation workspace**,
which had no Docker CLI/daemon. They passed in CI at the historical checkpoint
`713ecaa`; that says nothing about later commits. The job runs as advisory in CI
(it depends on external image pulls), so container acceptance for a given head
is claimed only from that job's own result on that head. Normal unittest
discovery reports these tests as skipped, never fake-green.
