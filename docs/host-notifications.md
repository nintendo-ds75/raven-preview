# Employee-host notifications and task clarification

Raven's default expert interaction happens in its authenticated inbox. Slack is
optional and carries a short task/decision link. Replies, approvals, or casual
messages in Slack do not answer or sign decisions in the default mode.

## Existing employee Slack MCP

The coding host can use an already authorized employee Slack connector. Raven
never copies its credentials and cannot invoke tools merely because the host
has them. The host must retain its own permission checks and verify the current
employee identity, workspace, and intended recipient using the connector.

1. Register the session with `bridge_host_event`, including reported tool names.
   Keep the returned `binding_id` and `task_id`.
2. Persist the expert request with `bridge_add_node`. It exists in Raven before
   notification. Read its exact current revision and linked owner.
3. Call `bridge_host_notification` with `action=capabilities` for the binding.
   Tool inventories are host-reported, never proof of available permissions.
4. Verify the recipient's Slack ID and email through the existing connector.
   The email must match the request's exact active Raven owner, and a known
   Raven Slack ID must also match. Do not use display names as identity.
5. When permitted to notify, call `action=claim` with the selected inventory
   tool, verified workspace/sender/recipient IDs, recipient email, node revision,
   and `permission_confirmed=true`. This field records the host's permission
   assertion; it does not grant permission or override its user policy.
6. Send the returned text only if `send_once=true`. Send it once using the
   employee connector, with the notification ID as a provider idempotency key
   where supported. Never attach evidence, account details, or credentials.
7. Call `action=receipt` with notification ID, content hash, state, and the
   provider-returned message reference if sent. `unknown` is a safe state for
   a timeout. Never blindly retry a send or invent a successful receipt.

Claim retries, concurrent claims, and restarts never grant another send. A lost
claim response can therefore leave a notification unsent. Resolve uncertain
outcomes through the connector or share the task link manually; this version
has no automatic resend/reconciliation command. Receipts are explicitly
host-reported, not independent Slack attestation. A configured server transport
blocks host claims to avoid duplicate bot/employee delivery.

No new Slack OAuth app, bot, admin setup, or credential exchange is required for
this host mode, but the existing connector must actually be authorized for the
workspace and sending operation. Workspace policies still apply.

## Optional server bot

A configured bot sends notification-only links by default, with a public HTTPS
Raven URL. Old queued rich payloads are reduced to a notification at send time.
An operator may explicitly set `slack_interaction_mode=conversation` to retain
the legacy conversational transport; it is not the default customer path.
Existing source-edit/deletion callbacks remain active for freshness invalidation.

## Read-only clarification

`POST /api/tasks/{task_id}/inquiry` accepts authenticated workspace viewers.
`POST /api/brief/inquiry` accepts a valid recipient task link. Both accept only
`question` and optional `focus_id`. The task and identity come from the route,
not request fields. They read only that task's context and decisions, reject
cross-task focus, and do not start coding sessions, create decisions, or sign.

Source visibility is checked before and after inference. Task text fingerprints
and reader/link validity are rechecked before returning. Model answers require
exact evidence citations and well-formed positive support checks; otherwise
Raven returns the evidence with an honest incomplete-reading notice.

## Limits

Normal notification links require a Raven account. Fresh expert account
onboarding is not yet a verified seamless path; the delivery protocol does not
create a user account or grant global workspace access. Existing recipient-link
routes are supported separately. Native source ACL checks remain workspace-level,
not proof of each user's upstream connector access. No live Slack send or live
Claude-host acceptance is established by the synthetic protocol tests.
