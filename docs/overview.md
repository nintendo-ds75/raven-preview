# How Raven works

The long version of the README: what a task looks like inside Raven, how answers
are remembered and reused, who gets asked and why, where your data goes, and what
this preview does and does not promise. For installation, see
[getting started](getting-started.md).

## The decisions inside a task

Start in Raven’s browser conversation, or connect your coding agent over MCP once. Give the agent a task such as “add usage-based pricing.” You do not need to supply file paths, owner names, or a list of decisions. The agent discovers those as it reads the repository. If Raven cannot place the task yet, the agent returns with the files it found. As it explores the work, Raven and the agent build a tree of decisions: which usage counts, what allowance to give, how to handle overages, and whether a particular customer needs different terms. An answer can reveal another question. The tree grows with the task.

Raven reviews the agent's investigation for missing judgment, searches records
and previous answers by keywords and a hashed embedding, with a local learned
embedding model (FastEmbed, BGE small) beside them when the image installs it,
and compares evidence to choose a first contact. The agent reviews those suggestions before asking
people. Local Claude Code and Codex use installed hooks and the resume adapter;
supported Work cloud chats and dots can subscribe to signed task events. See
[the investigation and wake-up flow](investigation-and-events.md) for setup
and limits.

Each decision can fall anywhere on this spectrum:

| What is available | What Raven brings to the owner | What the owner does |
| --- | --- | --- |
| **Direct evidence** | “The approved metering spec excludes internal test traffic. Should that rule apply to this rollout?” The source is attached. | Confirms its use here or corrects it. |
| **Informed prediction** | “Your earlier decisions protected prepaid value. We think unused purchased credits should roll over.” The reasoning is attached. | Reviews the proposal and signs or changes it. |
| **New judgment** | “Should this new partner get a custom credit policy?” The requirements and tradeoffs are attached. | Makes the decision that does not exist yet. |

**Owner signoff applies across the spectrum. A cache hit does not grant approval.**

Until signoff is recorded, an answer remains a proposal the agent can prepare against. It can do independent work and draft a change while waiting. If the owner corrects the answer, the agent revisits the affected work. Raven refuses its finish call while recorded decisions still need an answer or signature.

A company can explicitly enable already-approved standing rules to authorize matching cases without a fresh signature. That exception is **off by default**. Leave Automatic rules off when you want every reused answer reviewed again.

## Less coordination, more useful autonomy

Raven remembers the answer, who gave it, why, the scope it applies to, and its signoff. A referral teaches it who to ask. A correction changes what later tasks should rely on. Sources and interactions share one graph: a reused answer keeps the source versions and scope behind it. Connected search can retrieve a past decision through that relationship, and changed or expired evidence blocks stale reuse.

The goal is for more of the next task to start with the answer its owner would have given. More decisions become a cache hit. More proposals are right the first time. Agents can keep preparing work in the background, with fewer interruptions and less work to redo after review. People spend more of their attention on the genuinely new calls.

That is the long-term direction, not a measured guarantee that predictions become perfectly accurate. Owner review remains part of the workflow as memory improves.

Someone investigating a past change can also ask who decided it and why, with the original decision as a citation. Looking up history does not authorize a new use of that answer.

## Follow the whole task

The developer works in Raven’s conversation or stays in Claude Code, Codex, Cursor, or another MCP client. People can answer and sign in Raven’s authenticated inbox. Slack notifications point them to Raven, where they review context, ask read-only questions, and explicitly answer or sign. The default Slack path does not interpret replies as decisions. Slack is optional for a browser-only team. Teammates open the task overview to see:

* What was requested and who requested it.
* What the agent has learned, with answers and reasoning.
* Who owns each decision, who was asked, and what still needs signoff.
* Notes, referrals, corrections, delivery status, and the saved review of the submitted diff.

People can add context or required follow-up questions. Stakeholders can have read-only viewer accounts. A copied task link shares the location; it does not grant access to a shared instance.

The workspace **Tasks** view follows one run at a time. It shows recorded questions
and findings, where an answer came from, its current approval state, the people
contacted, and what still blocks progress. Evidence, source versions, referrals
and the chronological record expand when you need them. Findings can be useful
before they are approved; the page keeps those states separate. Work outside
Raven is not automatically captured.

On **Tasks → Start a task**, choose **Ask a question** to read connected evidence and earlier
decisions. This mode does not start a coding host, create a task, contact people,
or grant approval. It shows citations and what the sources leave open. Choose
**Start a task** when you want implementation and the human decision loop.

Browser tasks use a real Claude Agent SDK host with a saved session and Raven's
task tools. The Codex app-server adapter is version-pinned and experimental;
its live acceptance test is still pending. Host credentials and model settings
are separate from Raven's own inference. See [browser setup](browser-tasks.md)
for dependencies, execution limits, data flow, and the optional command sandbox.
The older **Ask Raven** action in Tasks creates a decision run and may contact an
owner. Its confirmation screen distinguishes that action from a read-only question.

### Outcomes: from evidence to one piece of work (preview)

**Outcomes** (Tasks → More → Outcomes) turns the loop around: instead of waiting for an agent to ask, Raven
reads evidence a person imported (a meeting transcript, notes, the connected
sources) and proposes one bounded piece of work with the quotes that justify it.
The proposal is an ordinary judgment node in the same inbox: the owner reviews
it, and only the exact signed scope can start a coding worker, which prepares
the change in a private copy and nothing more. A completed run is not the
business outcome; a person records that separately. It needs a configured model
backend and browser execution, an admin opts each repository into background
investigation, and it has not been exercised with a live provider or a real
meeting yet. [Outcomes and its limits](startup-os.md).

At completion, `bridge_export_proof` returns a portable review bundle containing
the exact submitted diff hash, decision revisions, recorded signers, scope,
citations and host-reported checks. Changed decisions mark the saved proof stale.
See [change proof and its trust limits](change-proof.md).

### Slack notifications, with an optional bot

An external coding host can send a short notification through its already authorized employee Slack MCP connector. The message appears from that employee and links to the task and decision in Raven. Raven does not copy the connector's credentials, bypass the host's permission checks, or gain access to tools simply because the host has them. The host must implement the [notification handoff](host-notifications.md); this is not an automatic connector available to every built-in browser host.

A separately provisioned Slack bot is an optional alternative. Both paths default to notification-only messages. People answer and sign in Raven, after reviewing the task overview and relevant evidence. Task-scoped questions are read-only and do not count as approval. Normal task links require Raven authentication; they do not grant access or create an account. Fresh expert onboarding is not yet a verified seamless path.

The older conversational Slack transport remains an explicit opt-in with `slack_interaction_mode=conversation`. Its optional personal task links are bearer capabilities: anyone holding one can act as its named recipient within that task's permissions until it expires or is revoked. They are not the default authenticated notification links and must not be forwarded. See [delivery and identity limits](host-notifications.md).

## Who does Raven ask?

Raven starts without a manually configured ownership map. Ingestion gives it the first signals. A configured Slack bot can import the directory; an external host using its own connector must verify the intended recipient before notifying them. It asks the strongest available contact and explains why. That is a first contact to confirm or refer, not a claim that a commit author has authority over every business decision.

People can ask for context, explain or correct an answer, or refer a decision in Raven. Read-only clarification stays separate from a recorded answer and explicit signoff. Raven remembers the answer and referral path; the final respondent becomes a first-contact signal for similar questions, not automatic proof of authority. With the optional conversational Slack transport enabled, these exchanges can also happen in Slack, including a configured triage channel. The default notification-only path keeps the decision in Raven. An unreachable owner remains an unresolved blocker, never permission to finish.

Manual ownership overrides and additional required approvers are available for teams that want them. They are not prerequisites. The host agent can check connection and delivery problems with `bridge_connection_status`, so the browser is optional for everyday work.

## Your instance and your data

You operate Raven and control its storage, networking, access and backups. Use a
local machine, your own servers or infrastructure in your chosen cloud account.
A shared deployment needs a reachable server, HTTPS and production authentication.
Docker uses PostgreSQL; direct Python installs can use SQLite. The application and
stored decision graph do not need a Raven-hosted service or a remote license check.

Self-hosted does not mean that enabled features have no outbound traffic. Model
calls go to your configured supported inference backend; Slack, GitHub, Teams and
optional context connectors use their respective services. Coding agents and
browser speech can also use their own providers. Your cloud and service providers'
access, processing and retention terms still apply. The built-in GitHub connection
uses a shared registered App by default; use a [customer-owned App](github-connection.md#optional-use-your-own-app)
when you need to control that registration too. See the [deployment data-flow details](../DOCKER.md#self-hosting-and-data-flow).

The shipped Raven application has no Raven-operated task-telemetry collector.
Support access is something you choose to provide: you can screen share, send a
trace you have reviewed and redacted, or invite a named support person. Viewer
access covers the workspace, not just one task. Exports are not automatically anonymous.

## What this preview can promise today

Raven proposes candidate decisions from the task, earlier decisions, and ownership signals. The host agent builds the tree as it explores the work. Discovery is not complete or guaranteed: an important question can still go unrecorded. Routing and reuse can also need correction.

The task view records what reaches Raven. The finish gate governs Raven's protocol, not your deployment system. Its model review compares a submitted diff with signed decisions; it does not prove the implementation correct or replace tests and code review. This preview is intended for a supervised pilot. See [what we have tested](verification.md) and the latest [context and host validation](context-host-validation-2026-10-06.md).

Slack is an optional notification channel; replies are processed only after an explicit conversational-mode opt-in. Teams incoming webhooks are outbound only; an optional [verified channel-bot reply preview](teams.md) requires a separate bot registration, pinned tenant/channel and explicit identity mapping. Its signed-fixture tests are not a live Teams certification. GitHub sync is built in. Jira records can be ingested through `bridge_import_record` from your host agent’s connectors, or through `POST /api/records`. A Raven-managed Slack transport requires a customer-provisioned bot; host-delegated notifications use the host’s existing employee connector instead. In the conversational transport, public-channel search uses Slack’s Real-time Search API during user interactions; results are transient and are never copied into Raven’s memory. Raven remembers the decisions people explicitly give it and their referrals. By default it does not backfill Slack or poll Jira. An administrator can opt in specific public Slack channels, Jira projects and documentation sites as [native context sources](context-sources.md), each attested as shared with every reader; `bridge_context_sources` reports what Raven can read. An optional customer-managed Airweave connection can supply synchronized ticket and document excerpts under its own source permissions; [setup and limits](context-connectors.md). Browser voice interviews are described below; native Slack calls and telephone integration are not shipped.

Browser voice interviews are available from a decision: spoken or typed answers,
grounded adaptive follow-ups when inference is configured, an editable readback,
and explicit authenticated confirmation. Without inference, clearly labeled
guided prompts remain available. Browser speech support and permission are
required for microphone input. This is not a native Slack huddle or telephone
integration; see [voice interview setup and limitations](voice-interviews.md).

The product is named Raven. The `bridge` Python package, `bridge_*` MCP tools, and `BRIDGE_*` settings retain their existing names for compatibility.

## For contributors running evaluations

The [evaluation audit tools](../evals/audit/README.md) freeze historical inputs,
record redacted MCP calls and evaluator interventions, and show what had been
observed at each point in a run. The [engineering plan](engineering-and-evaluation-plan.md)
defines contact and question-quality measures, host connection work, and the
context-layer design. It distinguishes implemented tooling from planned product work.
