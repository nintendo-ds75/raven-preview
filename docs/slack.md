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

Raven captures `record: <decision text>` in channels the bot can already see. Set
`slack_capture_repo` in workspace settings for that capture. Only an explicit,
non-thread `message` or `app_mention` enrolls its exact workspace/channel/message
identity. The authenticated event's workspace and the repository selected when
that original is accepted remain bound to it. Changing the capture repository
does not move an existing capture.

For those enrolled messages, subsequent edit callbacks update the same canonical
versioned source. Removing `record:` retires the evidence; adding it back in a
later edit restores the same source with a new revision. Deletion retires it
permanently. Material changes invalidate dependent decisions through the ordinary
source graph, while prior versions and completed proof bundles remain immutable.
Identical text advances only the local observation/ordering receipt. Imported
text never supplies a human sign-off.

Edits use the `ts` field inside Slack's nested message; deletions use `deleted_ts`. The separate
mutation occurrence (`event_ts`, or the envelope `ts` when absent) orders updates
as exact integer microseconds, with no float conversion or local-time fallback.
Missing, malformed, out-of-order or tied clocks do not replace the current
source. An edit cannot revive a deleted message.

A mutation arriving after an explicit original was accepted but before its import
commits waits durably under that exact message identity. It survives restart,
original-import retry exhaustion and duplicate original `message`/`app_mention`
deliveries. Retrying the failed original through the existing inbound retry action
can complete the capture. A failed original is never treated as a successful
import merely because its webhook was acknowledged.

Once a newer edit/delete is durably accepted, the previous captured snapshot is
immediately uncertain for current use. Source retrieval, automatic answer reuse,
sign-off and task completion refuse that supporting evidence until processing
commits, including during retry backoff, worker downtime or a disconnected
workspace. The gate follows source dependencies through completed historical
decisions to active consumers. It does not rewrite old signatures, versions or
proofs. Identical-content processing restores their usability without a new
material version or approval; changed content/deletion uses normal invalidation.
Older, duplicate and malformed callbacks do not create a new pending gate.

The queue retains at most 1,000 pending identities, with one newest mutation per
identity and at most 20,000 text characters per callback. Intermediate pending
edits may be coalesced; this is not a complete Slack edit history. Existing queued
updates are never evicted for capacity: a new identity receives HTTP 503 and must
be retried. Import failures remain visible in connection status and retry with
backoff, capped at five minutes. Disconnected-workspace updates wait for that
same workspace to reconnect. Invalid or oversized mutation callbacks are ignored
whole, never truncated into evidence.

Messages imported by callers or captured by older versions are not automatically
enrolled. Deliberately repost `record: ...` to create a fresh enrolled capture;
replaying an old applied callback or editing an unenrolled message cannot enroll
it. Explicit fresh capture retains old unscoped legacy sources as separate
history. Raven makes no history/channel reads, adds no subscriptions or scopes,
and never archives transient search results through this path. It cannot detect
missed callbacks or upstream changes while disconnected. Jira and other generic
imports still require a caller to refresh them; no Jira poller is added.

## Natural conversations and live search

Configure your inference backend in `./setup --configure`. Docker uses your Anthropic API key with `BRIDGE_MODEL_API=anthropic`. A local installation can use `BRIDGE_MODEL_API=claude-cli`. Without a backend, command shortcuts still work but Raven cannot converse about arbitrary questions.

The app belongs to the customer workspace and speaks as Raven. It does not use anyone’s personal Slack account. Its inference request includes the task, evidence, relevant task notes and recent messages in that person’s conversation. The CLI inference adapter has no coding tools or MCP servers and does not save model sessions.

Public-channel search uses `assistant.search.context` with the short-lived action token Slack supplies when a person messages the app. It runs on demand, not as a crawler. Search results and responses derived from them are not stored. An operator must not import Real-time Search results through the records API. Private-channel search and search initiated outside Slack require per-user OAuth; that connection is not included in this preview. A host agent may use its own authorized Slack connector for live context.

Raven keeps the conversation people have directly with it, confirmed decisions and referral outcomes in the customer’s database. Those improve answer reuse and first-contact routing. Learning is scoped by repository, question, path and stated task facts. It does not grant broad ownership just because a person was mentioned in a search result. Explicit ownership overrides remain available.

## What the person does

Raven sends the question, task context, evidence, and why it chose the person in a DM. Reply in that thread as you would to a colleague:

* “Why are we changing the current behavior?”
* “Keep the default for existing customers, but make it configurable for new ones.”
* “My complete replacement answer: keep the default, with an exception only for Acme.”
* “This is Priya’s call. Could you ask @Priya?”
* “Before finishing, check whether this also changes exports.”

Raven reads an answer, correction, referral, follow-up or rule back before applying it. Each read-back has a fresh short code: reply `confirm <code>` (or `yes <code>`) to approve that exact reading, `decline <code>` to drop it, or explain what to change. A bare `yes` in the root thread is ambiguous and does not approve anything. A natural confirmation works only when the authenticated transport supplies an exact reply-to reference to the delivered read-back. “OK”, “thanks” and “I’ll check tomorrow” do not sign anything. If the underlying answer changes, an older confirmation is refused. Pending read-backs, their message occurrence, and delivery proof survive a restart. Duplicate or out-of-order callbacks do not rebind consent to newer content. Missing, invalid, tied, or stale occurrence metadata fails closed; callbacks never borrow a signature-verification clock or processing time. Read-backs created before this binding existed must be restated.

With no pending read-back, command shortcuts still work: `answer: <decision> because <reason>`, `sign off`, or `not me @person`. If a read-back is pending, approval shortcuts also require its code or an exact reply-to binding. Add `just this one` to a referral to avoid teaching a reusable route. Signable answers are shown in full across Slack blocks. No inbox account is required.

Reply `context: <note>` to put context directly on the task for the coding agent, including in ordinary ask, reassignment, sign-off, and escalation threads. It is handled before inference or source-answer review, even if the body contains `because`, approval words, or other command-looking text. It does not approve, sign, assign, or make a rule, and a separate pending read-back keeps its existing semantics. The note body preserves the person's words after removing the prefix and outer whitespace (at most 4,000 characters); the full command including whitespace is limited to 4,096 characters. The task note keeps the original command and transport event/occurrence alongside the authenticated author and originating decision. Private clarification and chat are not automatically forwarded to the task.

If no contact matches the available evidence, Raven posts the question in the triage channel. Reply `I'll take this` or `ask @person`. This assigns the question only. The chosen person then answers or signs off; claiming it does not approve it or create broad authority.

The host calls `bridge_wait` and reads the answer through `bridge_get_tree`. It can continue independent work while it waits. `bridge_finish_task` refuses outstanding required answers and signatures. Evidence reuse still requires a signature unless an explicitly enabled, matching standing rule covers it.

Task notes are always in `bridge_get_tree.notes`. A wait returns notes added while it is running; pass the previous `observed_at` as `since` to also receive notes that arrived between calls. A wait without `since` does not replay existing notes. On an ordinary timeout, the guidance says “no new decision or forwarded task note is observable” and “this does not prove that nobody replied privately.” That is a generic visibility boundary, not a report about any particular private conversation.

### Complete authored answers and corrections

For a natural-language answer, inference classifies intent and whether the message is complete. Raven copies a complete authored answer verbatim into the read-back, including conditions, exceptions, negation and any inline reason. It does not replace the person's wording with a model summary or a more specific implementation. The received text remains in the conversation and confirmation evidence; outer transport whitespace is normalized as before. A separate reason field may be empty even when the complete answer includes a reason.

An incomplete amendment such as “keep that part, except for Acme” needs a complete replacement. Messages combining an answer with a referral, rule request or private discussion also need separate instructions. Raven asks for the complete answer through `answer: …` or the existing task review page instead of composing text for the person to sign. This is a conservative fallback; intent classification is not proof of human intent. Check the full reading before confirming it.

A complete replacement can correct an assumed or unsigned proposal. The ordinary ownership, revision, current-source review and explicit confirmation checks still apply. An oversized answer or source review is refused whole and uses the task review page; no shortened copy can be signed. This change does not alter previously recorded answers or pending historical evidence.

## Check it from your host agent

Ask the agent to call `bridge_connection_status`. It reports contact-sync errors, missing Slack configuration, the triage channel, and failed notifications. Node creation returns notification state, so an agent can distinguish “queued” from “sent” and “failed.” A successful MCP write alone is not proof that a message reached Slack.

An operator can refresh contacts with `POST /api/slack/sync` or the optional Connections & setup page. Slack callbacks are acknowledged before inference runs. Incoming messages and replies use durable queues; messages in a conversation are processed in order. Queued sends are retried. `bridge_connection_status` reports failed inbound events and replies as well as questions that could not be delivered. After a process dies, an in-flight event becomes retryable when its two-minute lease expires. A sync also retries messages that previously had no destination. Missing API scopes, ambiguous identities and unreachable people stay visible instead of silently dropping the question.

Directory matching and inferred routing can still pick the wrong first person. The reply and referral loop is how Raven corrects this. You do not need to configure owners in advance, and inferred contact evidence does not grant that person permanent authority over unrelated questions.

Slack API references: [directory pagination](https://docs.slack.dev/reference/methods/users.list/), [email scope](https://docs.slack.dev/reference/scopes/users.read.email/), [Events API](https://docs.slack.dev/apis/events-api/), [message edits](https://docs.slack.dev/reference/events/message/message_changed/), [message deletions](https://docs.slack.dev/reference/events/message/message_deleted/), [Real-time Search](https://docs.slack.dev/apis/web-api/real-time-search-api/), [app manifest](https://docs.slack.dev/reference/app-manifest/).

A referral read-back keeps the exact person identity shown when it was offered.
Confirmation does not re-resolve an old name or mention. If that contact is no
longer active or eligible, Raven keeps the proposal unsigned and asks for a fresh
referral. Contact lookup happens before the read-back, outside the approval write
transaction.
