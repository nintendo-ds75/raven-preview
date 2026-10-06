# Slack contract testing without a live workspace

Run from the repository root with Python 3.11+ and Git:

```sh
python evals/slack_contract_harness.py --output /tmp/raven-slack-contract.json
python -m unittest discover -s tests -p test_slack_contract_transport.py -v
```

The first command runs an authenticated customer MCP lifecycle and produces a
small JSON result. The second runs that lifecycle plus transport, failure and
security regressions. Both bind only ephemeral loopback ports, use temporary
SQLite databases, and contact no real Slack workspace or model service.
They need no Slack credentials, browser login, or manually maintained owner map.

## What crosses the real boundaries

The harness starts Raven's shipped HTTP server, `SlackTransport`, directory
sync, durable outbox and inbound worker. A synthetic repository is ingested
through the normal installation ingestion code. Its Git author and CODEOWNERS
email match the Slack directory fixture, allowing Raven to infer the first
contact. It does not pre-create, route, answer, or sign a decision in the store.

Every task operation is JSON-RPC over authenticated `POST /mcp`, using a
synthetic member's agent bearer token:

1. `initialize`, `notifications/initialized`, and `tools/list` (with the
   negotiated MCP version header and both supported Accept media types)
2. `bridge_start_task` and `bridge_add_node`
3. `bridge_wait`, `bridge_get_tree`, and `bridge_get_decision`
4. A refused `bridge_finish_task` while human authorization is absent
5. A completed `bridge_finish_task` after the attributed confirmation
6. `bridge_export_proof`, including after reopening the server and database

All incoming human actions are HTTP requests to `/webhooks/slack`. The harness
signs the exact raw bytes using `v0:{timestamp}:{body}`, HMAC-SHA256, and the
`X-Slack-Request-Timestamp` / `X-Slack-Signature` headers. Outbound DMs, thread
replies, directory requests, identity refreshes and searches reach a real
loopback HTTP server with documented Slack request/response shapes.

## The customer-facing MCP body

A coding client calls Raven, not the Slack double directly. For example:

```json
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"bridge_get_tree","arguments":{"task_id":"synthetic-task-id"}}}
```

Raven's normal tool result has this JSON-RPC/MCP wrapper; the text contains the
JSON task result, which the harness decodes and checks:

```json
{"jsonrpc":"2.0","id":4,"result":{"content":[{"type":"text","text":"{...task result...}"}],"isError":false}}
```

For an unapproved finish, the HTTP exchange still succeeds and the tool result
sets `isError: true` with the refusal in `content[0].text`. The lifecycle checks
that distinction instead of treating every HTTP 200 as task success.

## Slack contract represented

The fixture implements the subset Raven consumes, not every Slack endpoint.
Official sources were reviewed on October 6, 2026.

| Surface | Fixture contract | Official source |
| --- | --- | --- |
| `api.test` | `{ok:true}`, optional `args` echo, or `{ok:false,error,args}`; JSON and form bodies | [api.test](https://docs.slack.dev/reference/methods/api.test/) |
| `auth.test` | Bot identity including `team_id`, `user_id`, `bot_id`, `team`, `url`, and `ok` | [auth.test](https://docs.slack.dev/reference/methods/auth.test/) |
| `users.list` | `members` plus `response_metadata.next_cursor`; three deliberately short pages, terminal empty cursor; optional missing email | [users.list](https://docs.slack.dev/reference/methods/users.list/) |
| `users.info` / `users.lookupByEmail` | A user object under `user`; an `ok:false` error for an unknown identity | [users.info](https://docs.slack.dev/reference/methods/users.info/), [users.lookupByEmail](https://docs.slack.dev/reference/methods/users.lookupByEmail/) |
| `conversations.open` | String `users` request; `channel.id` response reused for a DM | [conversations.open](https://docs.slack.dev/reference/methods/conversations.open/) |
| `chat.postMessage` | String `channel`, text fallback, section blocks, optional parent `thread_ts`; `ok`, `channel`, `ts`, and `message` response | [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/) |
| `assistant.search.context` | `query`, short-lived `action_token`, channel/content filters; `results.messages` with `author_user_id`, `content`, and `permalink` | [assistant.search.context](https://docs.slack.dev/reference/methods/assistant.search.context/) |
| Events API | Outer `type`, `team_id`, `api_app_id`, `event_id`, `event_time`, `authorizations`; inner message `type`, `user`, `channel`, `thread_ts`, `text`, `ts`, `event_ts` | [Events API](https://docs.slack.dev/apis/events-api/), [message event](https://docs.slack.dev/reference/events/message/) |
| Callback authentication | Raw-body HMAC and five-minute replay window | [Verifying Slack requests](https://docs.slack.dev/authentication/verifying-requests-from-slack/) |
| Rate limits | HTTP 429 plus seconds in `Retry-After`; delay applies to the method in that workspace | [Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits/) |

The search action token is on the inner message event, as used by Slack's
[agent example](https://docs.slack.dev/ai/developing-agents/). The fixture does not
use the legacy `search.messages` API.

Fixture names, emails, IDs, source text, timestamps, tokens and permalinks are
synthetic. A fixture permalink is an exact citation oracle, not an accessible
real Slack message. Authorization headers are never logged. The synthetic
`action_token` is redacted from the fixture request trace.

## Covered behavior

- Cursor pagination continues even when a page is shorter than its requested
  limit. Bots, deleted members, and a foreign-workspace member are not imported;
  a contact without an email is still discovered.
- Git/Slack identity matching routes a question to its inferred owner. The real
  transport opens their DM and puts replies under the root `thread_ts`.
- A context question performs Slack search, names the source author, includes
  the returned permalink, and records no approval. Search text and action tokens
  are checked for absence from durable conversation, ingress, receipt and reply
  rows. This checks the synthetic canaries, not a general data-retention audit.
- A referral is read back, explicitly confirmed, and sent to another person's
  DM. The new owner supplies a decision; the decision remains unsigned until
  that same person confirms the readback.
- A different person cannot confirm the owner's pending answer. A provider
  error and a Slack search `missing_scope` response leave authorization closed.
- Invalid signatures, a modified raw body and stale timestamps receive HTTP
  401. A validly signed foreign-team callback is ignored. URL verification and
  bot-message filtering use the same endpoint.
- The pending readback survives reopening the HTTP server and database. A
  callback acknowledged while worker scheduling is paused remains queued and
  is applied on reopening. Repeating its `event_id` with Slack retry headers
  produces one applied decision and one acknowledgement.
- Real HTTP 429 responses exercise delayed callback replies, other replies to
  the same Slack method, and retryable identity lookups. HTTP 200 with
  `ok:false` is an API error, not successful delivery. Search failure is
  disclosed rather than represented as an empty successful search.
- The proof retains the complete submitted diff, the signed answer and its
  attribution after restart. Its integrity digest is checked; it is not a human
  digital signature and does not independently verify the host's test claims.

## Buttons are not implemented

Raven currently sends section blocks and asks for textual confirmation. The
harness asserts that no `actions` block is emitted, so a newly added button
cannot silently inherit a passing result. It does not manufacture a button
click or claim to test an interactive-actions handler. Shipping buttons requires
an additional signed form-encoded interaction test using Slack's
[`block_actions` payload](https://docs.slack.dev/reference/interaction-payloads/block_actions-payload/)
and the actual handler route.

## Deliberate simulation boundaries

- Slack's HTTP service is a handwritten, schema-shaped subset based on the
  official references above. It is not Slack's own executable validator. Live
  installation, OAuth scopes and workspace feature availability, Slack UI
  rendering, production rate limits and real delivery are not established.
- The conversation output boundary, `Client.complete`, is replaced. Slack
  conversation configuration is locally marked model-enabled while other model
  rungs remain disabled. The completion fixture returns scripted JSON for a context question, referral and answer. Raven still parses
  and validates that output, performs identity/authorization checks, constructs
  readbacks and applies confirmations. This verifies the model contract and
  surrounding control flow, not the quality of natural-language understanding.
  No real inference backend is exercised, and no model conformance claim is
  made about the synthetic diff.
- Installation setup creates a temporary workspace and an ephemeral test agent
  credential, ingests the fixture Git repository and syncs contacts. These are
  setup operations, separate from the exclusively HTTP-driven task lifecycle.
- Restarts reopen the HTTP server, workers and database in one Python process.
  Worker scheduling is paused for one enqueue to make durable replay
  deterministic. This is not an OS process kill, container restart, network
  partition or proof of exactly-once delivery after an ambiguous Slack accept.
- One Slack transport instance is exercised. Multi-process rate-limit
  coordination and multiple independent Raven deployments are outside this
  suite's scope.

Keep the boundary labels in any report that cites these tests. A pass means the
covered contracts passed against the synthetic service; it does not mean a live
customer workspace or a production model was tested.

## Required live-inference workflow acceptance

After secure Anthropic configuration and explicit approval, run the deeper
acceptance in that configured environment:

```sh
python evals/live_workflow_acceptance.py --run-live --max-calls 30 --output /tmp/raven-live-workflow.json
```

Without `--run-live`, or without the configured Anthropic key and enabled
inference, it refuses before creating the harness or contacting a provider.
The deterministic commands above retain their offline behavior. Never put a
provider key in this command, a source file, or a test artifact.

This driver uses the synthetic Slack service and authenticated HTTP MCP, with
real Raven inference and a real model-backed coding-host driver. It checks:

1. A rough task without supplied paths, owners or decisions produces live
   Raven candidates, and the coding host discovers a repository path/question.
2. A natural referral is read back and confirmed before contacting the new owner.
3. A novel randomized cohort and numerical threshold amend a natural answer;
   the readback preserves both, without silently signing it.
4. An attributed interview HTTP call asks a grounded, adaptive follow-up about
   a newly introduced unresolved staging environment. It remains an unapproved
   draft and grants no authority.
5. After explicit Slack confirmation, the coding host reads the signed answer
   via MCP and generates a restricted Python billing expression. The driver
   executes 16 positive/negative/boundary cases, including equality at the novel
   threshold, before finishing and exporting the actual change proof.

The default cap is 30 physical Anthropic HTTP attempts, including retries;
`--max-calls` is bounded to 1–100. Each provider network operation has a
45-second timeout. The JSON report records request counts, response statuses,
actual input/output/cache token usage, semantic results, and the failed stage
when applicable. It never includes headers, keys or an environment dump.
Background inference is stopped/drained under the same request gate at teardown.

Live mode does not patch model outputs or return canned answers. The coding
implementation is deliberately a constrained expression, not arbitrary code
execution or a general coding benchmark. Human inputs and Slack are still
synthetic; this run cannot establish actual human approval or live Slack access.
The interview's staging question remains a draft separate from the confirmed
production decision. Model review remains advisory.

Offline driver wiring, refusal, request-budget and semantic-oracle tests run as:

```sh
python -m unittest discover -s tests -p test_live_workflow_acceptance.py -v
```

Those unit tests explicitly inject a provider HTTP fixture. Their success is
not a substitute for a passing `--run-live` report.
