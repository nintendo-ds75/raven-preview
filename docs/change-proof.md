# Change proof for code review

After reading the current decision tree, finish a task with its complete unified
diff and the checks the coding host actually ran. Raven saves a portable evidence
bundle. Call `bridge_export_proof` with the same `task_id` to retrieve its JSON
bundle and Markdown review summary. Attach both beside the actual code change.

The bundle contains:

- The task, repository, requester and original goal
- The exact submitted diff, byte length and full SHA-256
- Decision answers, revisions, scope, rationale, recorded signers and attribution
- Source-decision citations and recorded evidence
- The model review saved at finish time, if any
- Test/build claims explicitly labeled as coding-host-reported

The finish response includes the proof ID and diff SHA-256. Repeating the same
export returns the saved bundle. Restarting Raven does not lose it. If a decision
or signature changes afterward, export returns `stale: true`; reread the tree,
update the code and finish again with the current complete diff. A separate task
cannot retrieve a proof merely by supplying the old task's client key.

The export's top-level `review` is the current advisory reading;
`bundle.payload.review` remains the immutable saved snapshot. `review_comparison`
lists the saved and current IDs and statuses separately. For example, a saved
`running` snapshot can share its ID with a current `done` reading. That does not
make the saved snapshot complete. Review IDs bind the inputs and are reused on
retry, so `same_review_id` is not an attempt identifier; it is null if either ID
is absent. `review_snapshot_current` compares the complete readings, not just
their IDs or statuses. Historical lookup should report both readings without
refreshing the proof. When preparing an updated proof, wait for a running review,
then finish again with the same complete diff and checks before exporting.

When summarizing a decision, use its recorded answer and rationale. Proposed
`options` are not an exhaustive list: a person can refine them or answer outside
them. Do not describe a refined answer as an original option unless the record
supports that. A citation points to evidence; its presence does not establish
that a summary accurately represents that evidence.

To check a downloaded JSON bundle offline, run
`python -m bridge.proof proof.json --diff changes.patch`, or call
`bridge.proof.verify(bundle)` from Python.
Also compare `payload.change.sha256` with the SHA-256 of the actual patch under
review. The JSON is content-addressed: changing its diff, decisions, scope or
checks without updating its digest is detected.

## What this establishes

This is a reproducible audit artifact, not a deployment gate. SHA-256 detects
changed contents; it does not authenticate the exporter or replace a person's
digital signature. Human approvals retain the identity and authorization basis
recorded by this workspace. Local-operator records remain local-operator records.
Another person could create a new bundle and hash, so compare its ID to the
trusted Raven instance before relying on its provenance.

The coding host supplies the diff and test results. Raven does not independently
run those tests, validate the complete checkout, or prove that a PR contains that
diff. Review the actual commit and execute independent checks. A model's review
may be pending or mistaken. Ordinary historical lookup creates no new approval,
and this artifact grants no authority for another task or customer scope.

Raven has one workspace trust boundary per database. Do not put unrelated tenants
in one shared instance and expect repository-scoped access controls.

## Tests

`PYTHONPATH=tests:. python -m unittest test_proof -v` covers exact-diff binding,
tampering, restart persistence, idempotency, stale decisions, premature export,
saved/current review separation, retry identity and human answers outside proposals.
The billing contract tests separately execute a reference implementation at the
allowance, seat, month, internal-test and scoped-legal-hold boundaries. Their
owners and policy decisions are explicitly synthetic.
