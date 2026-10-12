# Evidence for the 11 October customer loop

Read [the report](../../../docs/customer-loop-2026-10-11.md) first. Production revision `07e236ffaf6e9008c0c4e0d620b68989e8dc3380` stayed frozen.

This directory contains a redacted export, not credentials or a runnable customer configuration. Slack/Jira/docs services and people were synthetic. Docker/Postgres, the frozen urllib3 repository, Claude Code, inference, MCP, the browser and generated-code tests were real.

* `host-mcp.jsonl`: completed request/response exchanges recorded by the host's local MCP gateway, including actual hook, coding and resume calls.
* `audit.jsonl`: operator HTTP probes and simulated human messages, explicitly separate from autonomous host calls. Responses and errors are retained. Browser writes also appear in Raven's decision history and the saved snapshots.
* `slack.jsonl`: the actual outbound payloads accepted by the Slack double, including refused read-backs and formatting problems.
* `host-receipts.json`, `proof-first.json`, `trace-first.json`: host outcomes, the first saved code-review/proof result and task history. The proof includes a host-generated patch, not a private reference solution. Its original repository is [urllib3](https://github.com/urllib3/urllib3).
* `source-permission-probe.json`, `revision-probe.json`, `source-reapproved.json`: access-loss and content-change behavior. The former keeps old authorization; the latter invalidates it and records explicit reapproval.
* `tree-postcompletion.json`, `tree-resume-new-task.json`, `supervisor-*.json`: late follow-up and resume behavior. A successful process resume is not the same as completing the required follow-up.
* `cache-probes.json`, `external-key-lookup.json`, `work-item-link.json`: shared static/human context, exact-key lookup, reuse and fresh-signature requirements.
* `checks.json`, the test logs, `dom-suites.json` and `walkthrough.json`: measured checks, including failures.
* `fixture-boundary.json`, `network-boundary.json`, `repository-distribution.json`: historical cutoff, object inventory and host isolation. Synthetic policy fixtures are scenario inputs, not historical upstream policy.

The private oracle was hashed before the host ran. It is not published here. Two documentation assertions added to the executable checker at the end implemented requirements already present in that unchanged oracle; the report distinguishes the initial 8-check subset from the complete 10-check result.

Credentials, personal task links, CSRF values and any non-example email addresses are redacted. Redaction can change payload bytes: do not use these exports to verify an original human/source signature or treat the exported proof as an untouched canonical bundle. IDs and source relationships remain available for diagnosis. The raw records remain in the local evaluation workspace outside Git.

The public operator ledger was rehashed after redaction. `audit-heads.json` retains both the original private head and the public export head. To check the exported chain:

```sh
python3 - <<'PY'
import json
from pathlib import Path
from evals.audit.ledger import verify
p = Path('evals/results/customer-loop-2026-10-11')
rows = [json.loads(line) for line in (p / 'audit.jsonl').read_text().splitlines()]
expected = json.loads((p / 'audit-heads.json').read_text())['public_redacted_ledger_head']
print(verify(rows, expected))
PY
```

A matching hash detects changes relative to the retained head. It is not an independent signature, proof that every event was captured, or a substitute for inspecting the calls.

Reproduce the important failures on a disposable authenticated instance:

1. Import a shared Jira policy, bind a decision to its exact revision and sign it. Make the connector receive HTTP 403, then sync. Inspect source availability and current decision authorization. Separately test a Slack public channel becoming private.
2. Complete a signed task, add a required follow-up through its decision dialog, then use MCP to adopt that node. Enable the local supervisor for the stopped session and observe the same refusal and task binding.
3. Put two long related decisions behind a source-bound question. Send a complete owner answer through Slack. Inspect whether the read-back still fits and whether the source evidence remains intact.
4. Reply `Not me, please ask <@SYNTHETIC_USER> about compatibility.` to a current thread; compare with the exact supported referral form. Use real Slack timestamp precision in the double.
5. Temporarily make Slack unavailable during `bridge_host_event` reporting. Restore service and retry the exact report. Compare the stored report with actual outbound messages.

The run also corrected three harness mistakes: a duplicate-email Slack fixture, excessive timestamp precision, and an incorrect forwarded Host header. The first host attempt stopped on the Host-header error before model work. These are not counted as product defects. Restarting the local service double after its network namespace disappeared is also a harness repair; the report clearly separates the delivery behavior it exposed.
