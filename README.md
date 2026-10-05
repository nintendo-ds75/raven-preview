# Raven

**Your agents do the work. They come to you for judgment.**

A feature can be easy to code and still take weeks. Someone knows which usage is billable. Someone else owns pricing. A customer exception lives in a conversation. The developer ends up finding those people, carrying context between them, and turning their answers into a spec.

Raven helps discover those decisions as the task unfolds. It brings together what the company already knows, proposes answers where there is useful precedent, and asks the right people for the judgment and signoff the agent still needs. The agent can prepare the work in the background while people confirm or correct its assumptions.

## The decisions inside a task

Connect Raven to your coding agent over MCP once, then give the agent a task such as “add usage-based pricing.” You do not need to supply file paths, owner names, or a list of decisions. The agent discovers those as it reads the repository. If Raven cannot place the task yet, the agent returns with the files it found. As it explores the work, Raven and the agent build a tree of decisions: which usage counts, what allowance to give, how to handle overages, and whether a particular customer needs different terms. An answer can reveal another question. The tree grows with the task.

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

Raven remembers the answer, who gave it, why, the scope it applies to, and its signoff. A referral teaches it who to ask. A correction changes what later tasks should rely on.

The goal is for more of the next task to start with the answer its owner would have given. More decisions become a cache hit. More proposals are right the first time. Agents can keep preparing work in the background, with fewer interruptions and less work to redo after review. People spend more of their attention on the genuinely new calls.

That is the long-term direction, not a measured guarantee that predictions become perfectly accurate. Owner review remains part of the workflow as memory improves.

Someone investigating a past change can also ask who decided it and why, with the original decision as a citation. Looking up history does not authorize a new use of that answer.

## Follow the whole task

The developer stays in Claude Code, Codex, Cursor, or another MCP client. People receive questions and signoff requests in Slack DMs and answer in the same thread. They do not need a Raven account. The web inbox and task overview are optional. Teammates open the task overview to see:

* What was requested and who requested it.
* What the agent has learned, with answers and reasoning.
* Who owns each decision, who was asked, and what still needs signoff.
* Notes, referrals, corrections, delivery status, and the saved review of the submitted diff.

People can add context or required follow-up questions. Stakeholders can have read-only viewer accounts. A copied task link shares the location; it does not grant access to a shared instance.

### The task page in a Slack message

People answer in the Slack thread, without a Raven account. A thread holds the question, though, not the task around it, so each Slack direct message also carries that person's own link to the task page. It opens without signing in and shows the decision waiting on them, who requested the task and what they asked for, the other decisions and who made them, who was contacted, and what happened. They can answer, sign, hand the decision on, or add context for the agent from there too. Raven's usual permission check still decides whether their answer counts. A link opens only its own task, expires after 14 days, and acts as a member, never as an administrator. Anyone holding it can act as that person, so the page asks people not to forward it. Someone who wants the web inbox can create an account from the page in one step.

An administrator can turn the link off under People & ownership; messages then read as they did before.

## Who does Raven ask?

Raven starts without a manually configured ownership map. Ingestion gives it the first signals, and Slack’s directory makes those people reachable. It asks the strongest available contact and explains why. That is a first contact to confirm or refer, not a claim that a commit author has authority over every business decision.

People can ask for context, explain an answer, correct it, or say who to ask next in ordinary Slack messages. Raven uses your inference backend to follow the conversation and reads decisions back for confirmation. It remembers both the answer and the referral path. When the final person answers, that becomes a first-contact signal for similar questions; a declined contact is no longer the default for that question. If no contact can be reached, the question goes to your Slack triage channel, where someone can say `I'll take this` or `ask @person`. It never silently treats an unanswered question as permission to finish.

Manual ownership overrides and additional required approvers are available for teams that want them. They are not prerequisites. The host agent can check connection and delivery problems with `bridge_connection_status`, so the browser is optional for everyday work.

## Install locally with Docker

You need Docker Desktop, or Docker Engine with Compose v2, running on your machine. The first build needs internet access. No host Python, Node, or AWS account is required.

Clone the public preview:

```sh
git clone https://github.com/nintendo-ds75/raven-preview.git
cd raven-preview
./setup
```

Setup builds the app, starts PostgreSQL, and prints the local URL. It normally opens the browser for you. If port 7333 is in use, run `./setup --port 7444` instead.

1. **Connect Slack.** Install your workspace’s Raven app and run `./setup --configure --project /path/to/your/code` to save its bot token, signing secret, and triage channel. Follow [Slack setup](docs/slack.md), including its required scopes and Events API URL. Raven imports the member directory automatically. Nobody needs to enter Slack IDs or set up owners.
2. **Give Raven context.** Connect GitHub, ingest a local clone, or have your host agent import records from its connected Jira and document tools. Slack search provides live context during a Slack conversation. Raven uses CODEOWNERS, commits, reviews, record authors, and earlier answers to infer who to ask. No hand-built ownership map is required.
3. **Connect your coding agent over MCP.** `./setup --configure --project /path/to/your/code` can configure Claude Code, Codex, or Cursor. Add the supplied Raven instruction to the project’s `CLAUDE.md` or `AGENTS.md`. You then give the agent ordinary tasks; it discovers paths and decisions itself.
4. **Run one real task.** Raven DMs the inferred contact with the question and evidence. They answer, sign off, or refer it in Slack. The agent waits, reads the answer, and continues. You can watch in the browser, but you do not need to.

For setup without creating a personal account or opening the browser:

```bash
./setup --workspace "Your team" --configure
```

Natural Slack conversations require an inference backend: select Anthropic in setup and supply your API key, or use the Claude CLI backend in a local installation. Without inference, explicit answer and referral commands still work.

The operator configures the instance once. Recipients need only their existing Slack account. A personal Raven account is optional for viewing the web overview or using the inbox. Local Docker setup keeps separate operator and limited MCP credentials; an agent credential cannot sign for a person. For cloud hosting, use your own deployment secrets and a Slack-reachable HTTPS URL.

For a local repository, supply its absolute path and its GitHub identity:

```sh
./setup --repo /absolute/path/to/your/repository --repo-name your-org/your-repository
```

For an illustrative workspace without a live coding agent:

```sh
./setup --demo
```

Inference is optional for exploring the interface and deterministic routing. Model-assisted discovery, proposals, and diff review need a configured provider. The Docker image uses your Anthropic API key; it does not inherit a Claude CLI login from the host.

```sh
./dev status   # Check the app and database
./dev logs     # Follow application logs
./dev down     # Stop the stack and keep its data
./dev up       # Rebuild or restart with the saved configuration
```

[Docker, hosting and backups](DOCKER.md) · [Accounts and invitations](docs/user-onboarding.md) · [Guided pilot](docs/guided-pilot.md) · [Technical reference](docs/reference.md)

## Your instance and your data

Docker packages the software. You supply the machine it runs on. A local trial runs on your laptop; a shared deployment needs a reachable server, HTTPS, authentication, and backups. Docker uses PostgreSQL. Direct Python installs can use SQLite.

Configured inference providers and connectors receive the context needed for their calls. Raven does not automatically send us task telemetry or give us access to your instance. You can screen share, send a trace you have reviewed and redacted, or invite a named support person. Viewer access covers the workspace, not just one task. Exports are not automatically anonymous.

## What this preview can promise today

Raven proposes candidate decisions from the task, earlier decisions, and ownership signals. The host agent builds the tree as it explores the work. Discovery is not complete or guaranteed: an important question can still go unrecorded. Routing and reuse can also need correction.

The task view records what reaches Raven. The finish gate governs Raven's protocol, not your deployment system. Its model review compares a submitted diff with signed decisions; it does not prove the implementation correct or replace tests and code review. This preview is intended for a supervised pilot. See [what we have tested](docs/verification.md).

Slack is the normal notification and reply channel. Teams is outbound only. GitHub sync is built in. Jira records can be ingested through `bridge_import_record` from your host agent’s connectors, or through `POST /api/records`. The customer installs Raven as an internal Slack bot. Public-channel search uses Slack’s Real-time Search API during user interactions; results are transient and are never copied into Raven’s memory. Raven remembers the decisions people explicitly give it and their referrals. It does not backfill Slack or include a Jira polling connector. The voice conversation shown in the demo is a product direction, not a feature shipped in this preview.

The product is named Raven. The `bridge` Python package, `bridge_*` MCP tools, and `BRIDGE_*` settings retain their existing names for compatibility.
