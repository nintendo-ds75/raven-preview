# Auditable replay tools

These tools prepare and inspect evaluations. They do not modify Raven's approval
rules or automatically grant a coding host access to a customer's systems.
See the [engineering plan](../../docs/engineering-and-evaluation-plan.md) for the
host/connector architecture and scoring rubric.

## Freeze the visible inputs

Prepare a plain task brief and a JSON provenance file containing:

```json
{
  "kind": "blinded_reconstruction",
  "reviewer": "reviewer-1",
  "reviewed_brief_sha256": "SHA256_OF_EXACT_BRIEF_BYTES",
  "no_solution": true,
  "no_expected_people": true,
  "no_future_references": true
}
```

Use `preexisting_request` when the exact historical request is available. These
are evaluator attestations, not machine proof that prose contains no hints.

```sh
python -m evals.audit freeze --repo /evaluator/full-repo \
  --baseline FULL_COMMIT_SHA --cutoff 2025-10-06T00:00:00Z \
  --brief /evaluator/task.txt --brief-provenance /evaluator/brief-review.json \
  --records /evaluator/historical-records.json --out /evaluator/sealed
python -m evals.audit verify-fixture /evaluator/sealed
```

`--records` is optional. Each record must contain `id`, `provider`, `namespace`,
`body`, `version`, `created_at`, `updated_at`, `snapshot_at`, `provenance`, and a
nonempty `acl` audience list. All three timestamps must be timezone-aware, in
order and no later than the cutoff. The entire record is hashed, including any
status, author or comments supplied. Current edited bodies with old creation
dates are not valid historical snapshots.

Freeze produces a detached checkout in its own Git object store, without a
remote, future refs, original fetch location or object alternates. Every stored
object must belong to the chosen baseline's reachable history. Verification
also detects changed briefs, records and working trees. Existing destinations
are refused. Do not freeze into the source repo.

The **runner must enforce isolation**: mount only a writable copy of `checkout`
and explicitly permitted source snapshots into the host; mount no evaluator
directory, answer keys or full clone. Disable live sync. Limit network egress to
inference and Raven. Git closure checks do not prevent a host with unrestricted
filesystem/network access from fetching the answer elsewhere. Verify before
copying, retain the manifest digest externally, and record later controlled
source advances as separate events. Model-training contamination is not addressed.

## Freeze the scoring contract

```json
{
  "reviewer": "reviewer-2",
  "decisions": [{
    "id": "retry-format",
    "intent": "Which delay formats should be accepted?",
    "acceptable_contacts": [{
      "person_id": "engineer-123",
      "role": "knowledgeable",
      "evidence": "Historical review of delay parsing",
      "evidence_at": "2025-08-01T00:00:00Z"
    }]
  }]
}
```

Roles are `knowledgeable`, `referrer` or `decider`. Each contact needs pre-cutoff
evidence. An empty set means routing is unlabelled, not automatically wrong.
An explicit `decisions: []` can label a task expected to need no human decision;
omitting the list is invalid. Count needless questions separately from discovery.
Keep this file and the ledger outside host/Raven-readable mounts.

```sh
python -m evals.audit pin-rubric --rubric /evaluator/rubric.json \
  --cutoff 2025-10-06T00:00:00Z --ledger /evaluator/audit.jsonl
```

Pinning after a host call, pinning twice, or grading with a different rubric is
refused. Retain the returned hash externally before running.

## Record calls and interventions

The existing `evals/real_oss_remote/mcp_proxy.py` preserves its ordinary trace
format and accepts `BRIDGE_EVAL_AUDIT=/evaluator/audit.jsonl`. It appends the exact
JSON protocol observations after credential redaction, with a unique proxy
session ID. It uses the existing `BRIDGE_EVAL_URL`, `BRIDGE_EVAL_TRACE` and
`BRIDGE_TOKEN` settings. Give the proxy those values through the trusted launcher,
not the host prompt. A separate proxy container/sidecar is required if the host
must not read its files or credential-bearing process environment.

`evals/real_oss_remote/run_host.py` now enables this ledger and records prompts,
simulated-human/evaluator interventions, newly observed internal events, changed
decision/source snapshots, final tree and task history. Snapshot reads are
observations from separate reads, not an atomic database snapshot. Its old
fixture/label design remains a legacy cohort, not a new sealed acceptance run.
This particular proxy is a serial JSON HTTP client; it does not claim coverage
of streamed progress, parallel calls or unrelated host file/shell tools.

For a custom runner, append human input, source imports, connector lookups,
failed operations and host lifecycle events **when they happen**, using `Ledger`
or the CLI. Raw source/decision snapshots can be recorded as JSON observations.
Record the outcome separately from the attempted action. Never silently replace
an earlier entry with a successful retry.

```sh
python -m evals.audit record --ledger /evaluator/audit.jsonl \
  --kind human_reply --actor simulated-owner --data /evaluator/reply.json
python -m evals.audit view --ledger /evaluator/audit.jsonl \
  --out /evaluator/audit.html --head EXTERNALLY_RETAINED_HEAD
```

The viewer has a timeline, event-position control, filter, raw observations, and
the latest observed source/decision links at that point. It is a local file, not
a new unauthenticated customer endpoint. Missing observations remain unknown.
Run it only on data the viewer is permitted to see. Do not host customer exports
publicly. Provider credentials/common bearer formats are redacted, but this is
not a general personal-data anonymizer; preview/redact customer content before
sharing. Private model reasoning is not collected.

The append-only interface serializes writers with a file lock and verifies its
hash chain. Changes and truncation are detectable against an **independently
retained head**. Anyone with write access can rewrite a chain and its local head;
it is not cryptographic proof of the evaluator's identity. The small-cohort JSONL
implementation rereads the chain on append and is not a large-scale telemetry store.

## Check the patch and score the run

```sh
python -m evals.audit attest-patch --repo /host/workspace --baseline FULL_BASE_SHA \
  --patch /evaluator/host-submitted.diff --ledger /evaluator/audit.jsonl
python -m evals.audit score --rubric /evaluator/rubric.json \
  --observations /evaluator/adjudication.json --ledger /evaluator/audit.jsonl
```

Patch attestation fails if bytes differ, the submitted patch does not apply, or
untracked nonignored files remain. It uses a temporary index and does not stage
or repair the host's work. Ignored files and behavior beyond the diff need their
own checks. Capture native test commands, exit codes and logs independently;
this command does not turn a model's test claim into a test result.

An adjudication is a JSON array with `decision_id`, boolean `discovered`, optional
`first_contact`, `handoffs`, `authorization`, `judge`, `evidence_events` (ledger
sequence numbers), and `question_quality`. The six question keys are `necessary`,
`answerable`, `context_sufficient`, `neutral`, `readable`, `scope_clear`. Values
are `pass`, `fail`, or `unknown`; scored question axes require a judge and trace
references. Authorization is a separate pass/fail/unknown result. Missing axes,
unlabelled contacts and missing decisions are reported explicitly. No combined
success number is manufactured.

Keep a locked final holdout separate from development tasks. Retain every
attempt and interruption; a recovered run cannot replace its failed first attempt.
