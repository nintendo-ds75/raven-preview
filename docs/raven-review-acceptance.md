# Retained-host advisory review acceptance

`python -m evals.raven_review_acceptance` is blocked unless the controller supplies
`--run-live`, `--source-run`, `--output-dir`, and `--credential-file`. Run it only
in the original authorized runtime, where the saved private credential is read
in place. An optional `--workspace-id` selects the already-authorized Anthropic
workspace. The key is never written to reports or sent to the Raven MCP server.

The source directory must contain the synthetic host `result.json`,
`fixture/raven.db`, and `fixture/checkout/billing/usage.py`. The runner verifies
that the actual checkout patch matches the retained report, independently checks
16 boundary cases, and constructs one adversarial `>` to `>=` mutation. Each
case starts from a separate read-only SQLite backup of the source. No source
files are written. Existing advisory events, signed decisions, and source/access
pins remain intact; the production cache must refresh when inference is enabled.

The same authenticated agent MCP runtime calls finish, reads the current review,
and exports the exact-diff proof. A terminal completed review is finished again
to refresh the saved proof and confirm an unchanged enabled cache hit without
more inference. A failed review is not retried. Unauthenticated MCP and agent
signoff attempts must remain refused. No Slack or GitHub transport is configured.
Only provider transport is routed through the official-endpoint budget gateway;
model outputs and the production review validator are unchanged.

## Bounded command

The controller chooses fresh output and source directories:

```sh
python -m evals.raven_review_acceptance \
  --run-live \
  --source-run /path/to/retained-synthetic-host-run \
  --output-dir /path/to/new-review-result \
  --credential-file /path/to/private/saved-anthropic-key \
  --model claude-sonnet-4-6
```

The shared ceiling is 8 physical provider requests (including retries), 300,000
serialized input bytes, and 12,000 reserved output tokens. The explicit
`--max-output-tokens` option may raise the output ceiling to at most 20,000 when
that larger budget is authorized. Normally the two reviews use at most six
1,200-token stages. The production adapter may discover a thinking model and
reserve an extra 8,000 tokens per later call; either ceiling can therefore yield
an honest inconclusive result. No automatic budget expansion occurs.

The run has a 240-second target, reserving its last 65 seconds for in-flight
transport and review-worker cleanup. After the execution deadline no new model
request is admitted. A non-quiescent cleanup is a failure, never a pass, and
ambient credentials/transport are not restored under a running worker.

`result.json` reports each model verdict, exact input hashes, signed-input
bindings, source preservation, cache reuse, proof snapshot freshness, provider
usage, and cleanup. `passed` requires a completed positive review of the correct
patch plus a departure or located counterexample for the wrong boundary.
`inconclusive` is distinct from detecting the bug. Any `follows` verdict for the
wrong boundary is a hard failure, even in an otherwise partial response.

This is a separate live advisory-review diagnostic. It is not a new autonomous
Claude-host E2E run, proof of general review reliability, or independent code
correctness evidence. The deterministic boundary oracle is reported separately.

## Offline tests

```sh
python -m unittest discover -s tests -p test_raven_review_acceptance.py -v
```

These use a synthetic HTTP transport response, never a real provider or a saved
credential. They exercise the real review runtime and validator, including
positive, false-positive, incomplete, unsigned-source, deadline, budget-option,
and credential-refusal cases. Offline transport results are explicitly labeled.
