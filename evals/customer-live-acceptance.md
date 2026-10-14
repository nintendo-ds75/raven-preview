# Bounded Claude-host customer acceptance

This is an opt-in live integration evaluation, not a claim that every customer deployment or real Slack workspace has been accepted.

## Offline prerequisite

```sh
python3 evals/customer_acceptance_preflight.py --output /tmp/raven-preflight.json
PYTHONPATH=tests:. python3 -m unittest test_customer_acceptance_preflight test_customer_provider_gateway test_claude_mcp_acceptance
python3 -m pip install -r requirements-hosts.txt
```

The preflight and controller tests use synthetic actors, stores and model doubles. Passing them is not a live-provider result.

## Live run

Use an isolated test runtime with the pinned official Claude Agent SDK. Supply a new, empty evidence directory. Enter the key only into the hidden terminal prompt; never place it in a command, chat, file or shell history.

```sh
python3 -m evals.claude_mcp_acceptance --run-live --model claude-sonnet-4-6 --output-dir /tmp/raven-acceptance-new --workspace-id YOUR_WORKSPACE_ID --timeout 600
```

The workspace identifier is not a credential. Omit it for a key that does not need a workspace header.

Entering the key runs:

1. One direct production Raven inference operation, requesting 32 output tokens. Production retry behavior permits at most four HTTP attempts for retryable errors.
2. The actual official Claude Agent SDK, with native shell/file tools disabled, using Raven's stdio MCP proxy and a second tightly restricted synthetic-repository MCP server.
3. A required task-scoped natural-language inquiry using actual Raven inference and cited task evidence.

The real key remains in the controlling process and is sent only to the official Anthropic endpoint. The host and MCP subprocesses receive a random local placeholder, not the real provider key. The test-only loopback gateway forwards actual provider requests/responses unchanged and reserves a budget before each request. Raven's inference transport is replaced with this gateway only for the scoped inquiry; routing, evidence, human authority and durable-task behavior are not replaced by model responses invented by the gateway. The initial production call uses the production transport directly.

Gateway defaults: at most 24 physical requests, 1,500,000 serialized input bytes, and 24,000 total requested maximum output tokens. These are conservative request ceilings, not a guarantee of a dollar cost or a billed input-token limit. The official SDK additionally receives a $2 session budget. The direct production operation is outside the gateway ceiling. Usage and limits are recorded; stop on failed authentication, provider errors or an exhausted gateway budget.

## What the live lifecycle checks

- Actual host reads static source context and a differently scoped prior judgment before escalating.
- The first question routes to the seeded authorized owner.
- Optional Slack fixture sends a notification and deep link; Slack replies do not answer or sign the request.
- The host is genuinely blocked in `bridge_wait`; provider usage remains unchanged during the observed pending window.
- The host process is stopped, Raven restarts, and the durable task remains intact without blocked polling inference.
- An agent credential cannot sign as the human. A test-controlled human explicitly signs a revision-bound production judgment.
- The same Claude session resumes, reads current state and implements the signed judgment in the actual fixture file.
- Independent hidden boundary checks and the exported proof verify the actual patch, rather than trusting the model's completion claim.
- A live task inquiry returns cited evidence without adding decisions, authorization or signoffs.

The host-phase Raven discovery/decision ladder is deterministic and inference-disabled. Source freshness, cache invalidation and broader ACL/authority cases are separately labeled deterministic regressions. The coding task is intentionally constrained to one expression-only policy function. Real Slack delivery, fresh-expert OAuth onboarding, unrestricted repository coding and all model-driven cold-start routing are not validated by this runner.

## Evidence and cleanup

Read the new directory's `result.json`, MCP trace and actual fixture patch. A failure or blocked stage is not acceptance. Never include credentials in reports. Stop the temporary host and test sandbox after the result is captured; preserve the user's existing sandbox data. The key is not persisted, so a later run requires a new user-controlled entry.
