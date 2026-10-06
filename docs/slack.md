# Use Raven through your agent and Slack

The coding agent uses MCP. People receive DMs and answer in Slack. The web overview is optional, and recipients do not create Raven accounts or fill in an ownership map.

One workspace operator still needs to install the Slack app and connect the sources Raven may read. Slack must be connected for notifications to leave Raven. Running Docker alone does not connect Slack.

## One-time setup

Start the instance first with `./setup --workspace "Your team"`, then give it a Slack-reachable HTTPS address. Keep it running while configuring the app so Slack can verify the callback.

1. Create a Slack app for your workspace using [slack-manifest.json](slack-manifest.json). Replace the example request URL with `https://your-raven-host/webhooks/slack` before saving it. That URL must be reachable by Slack. A local-only `localhost` URL will not receive replies; use an HTTPS deployment or a tunnel you control.
2. Install the app. Save its Bot User OAuth Token and Signing Secret through `./setup --configure`. The bot needs `chat:write`, `im:write`, `im:history`, `channels:history`, `users:read`, `users:read.email`, `app_mentions:read`, `assistant:write`, and `search:read.public`. The manifest enables the agent view and a writable Messages tab. Reinstall after changing scopes. These are bot scopes, not a token that impersonates you.
3. Create or choose a public workspace channel for questions Raven cannot route. Invite the bot and set its channel ID as `SLACK_FALLBACK_CHANNEL`. Pick a channel whose membership may see the task context. Raven does not join channels automatically.
4. Set `BRIDGE_PUBLIC_URL` to the instance's external HTTPS URL. Enable Events API subscriptions to `message.im`, `message.channels`, and `app_mention`. Slack verifies the request URL; Raven validates each event's signing secret.
5. Start with `./setup --workspace "Your team" --configure`. The workspace and limited MCP credential can be created without a browser account. Connect your host agent using the wizard. Keep the Raven service running so it can receive replies while an agent waits.

For a local Docker instance, the admin credential is available through `./dev login`; it is for the operator, not the agent. The setup wizard configures agents with the separate limited credential. For a shared deployment, use the authentication guidance in [DOCKER.md](../DOCKER.md).

## Contact discovery and ingestion

Raven imports active full members through Slack's paginated directory API when the delivery worker starts and every 15 minutes. A newly mentioned person is looked up when the referral arrives. Directory entries are contacts, not browser accounts, and Slack workspace admins do not automatically become Raven admins. Existing identities are linked by Slack ID or exact email, never by a name alone.

Connect GitHub or ingest a local clone for CODEOWNERS, commits, blame, and review history. The host agent can also call `bridge_import_record` with a ticket or document it retrieved through its own connected tools. Include the original author, status, permalink, and related paths. A matched record's author can be an inferred first contact. Imported text is evidence, never signoff.

Raven captures `record: <decision text>` in channels the bot can see. Set `slack_capture_repo` in workspace settings for that capture. It does not backfill your whole Slack workspace automatically. Jira currently uses record ingestion rather than a built-in Jira polling connector.

## Natural conversations and live search

Configure your inference backend in `./setup --configure`. Docker uses your Anthropic API key with `BRIDGE_MODEL_API=anthropic`. A local installation can use `BRIDGE_MODEL_API=claude-cli`. Without a backend, command shortcuts still work but Raven cannot converse about arbitrary questions.

The app belongs to the customer workspace and speaks as Raven. It does not use anyone’s personal Slack account. Its inference request includes the task, evidence, relevant task notes and recent messages in that person’s conversation. The CLI inference adapter has no coding tools or MCP servers and does not save model sessions.

Public-channel search uses `assistant.search.context` with the short-lived action token Slack supplies when a person messages the app. It runs on demand, not as a crawler. Search results and responses derived from them are not stored. An operator must not import Real-time Search results through the records API. Private-channel search and search initiated outside Slack require per-user OAuth; that connection is not included in this preview. A host agent may use its own authorized Slack connector for live context.

Raven keeps the conversation people have directly with it, confirmed decisions and referral outcomes in the customer’s database. Those improve answer reuse and first-contact routing. Learning is scoped by repository, question, path and stated task facts. It does not grant broad ownership just because a person was mentioned in a search result. Explicit ownership overrides remain available.

## What the person does

Raven sends the question, task context, evidence, and why it chose the person in a DM. Reply in that thread as you would to a colleague:

* “Why are we changing the current behavior?”
* “Keep the default for existing customers, but make it configurable for new ones.”
* “Actually, the exception is only for Acme.”
* “This is Priya’s call. Could you ask @Priya?”
* “Before finishing, check whether this also changes exports.”

Raven reads an answer, correction, referral, follow-up or rule back before applying it. Each read-back has a fresh short code: reply `confirm <code>` (or `yes <code>`) to approve that exact reading, `decline <code>` to drop it, or explain what to change. A bare `yes` in the root thread is ambiguous and does not approve anything. A natural confirmation works only when the authenticated transport supplies an exact reply-to reference to the delivered read-back. “OK”, “thanks” and “I’ll check tomorrow” do not sign anything. If the underlying answer changes, an older confirmation is refused. Pending read-backs, their message occurrence, and delivery proof survive a restart. Duplicate or out-of-order callbacks do not rebind consent to newer content. Missing, invalid, tied, or stale occurrence metadata fails closed; callbacks never borrow a signature-verification clock or processing time. Read-backs created before this binding existed must be restated.

With no pending read-back, command shortcuts still work: `answer: <decision> because <reason>`, `sign off`, or `not me @person`. If a read-back is pending, approval shortcuts also require its code or an exact reply-to binding. Add `just this one` to a referral to avoid teaching a reusable route. Signable answers are shown in full across Slack blocks. No inbox account is required.

If no contact matches the available evidence, Raven posts the question in the triage channel. Reply `I'll take this` or `ask @person`. This assigns the question only. The chosen person then answers or signs off; claiming it does not approve it or create broad authority.

The host calls `bridge_wait` and reads the answer through `bridge_get_tree`. It can continue independent work while it waits. `bridge_finish_task` refuses outstanding required answers and signatures. Evidence reuse still requires a signature unless an explicitly enabled, matching standing rule covers it.

## Check it from your host agent

Ask the agent to call `bridge_connection_status`. It reports contact-sync errors, missing Slack configuration, the triage channel, and failed notifications. Node creation returns notification state, so an agent can distinguish “queued” from “sent” and “failed.” A successful MCP write alone is not proof that a message reached Slack.

An operator can refresh contacts with `POST /api/slack/sync` or the optional Connections & setup page. Slack callbacks are acknowledged before inference runs. Incoming messages and replies use durable queues; messages in a conversation are processed in order. Queued sends are retried. `bridge_connection_status` reports failed inbound events and replies as well as questions that could not be delivered. After a process dies, an in-flight event becomes retryable when its two-minute lease expires. A sync also retries messages that previously had no destination. Missing API scopes, ambiguous identities and unreachable people stay visible instead of silently dropping the question.

Directory matching and inferred routing can still pick the wrong first person. The reply and referral loop is how Raven corrects this. You do not need to configure owners in advance, and inferred contact evidence does not grant that person permanent authority over unrelated questions.

Slack API references: [directory pagination](https://docs.slack.dev/reference/methods/users.list/), [email scope](https://docs.slack.dev/reference/scopes/users.read.email/), [Events API](https://docs.slack.dev/apis/events-api/), [Real-time Search](https://docs.slack.dev/apis/web-api/real-time-search-api/), [app manifest](https://docs.slack.dev/reference/app-manifest/).

A referral read-back keeps the exact person identity shown when it was offered.
Confirmation does not re-resolve an old name or mention. If that contact is no
longer active or eligible, Raven keeps the proposal unsigned and asks for a fresh
referral. Contact lookup happens before the read-back, outside the approval write
transaction.
