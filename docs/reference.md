# Technical reference

Raven retains the `bridge` package, MCP tool names and environment variables for compatibility.

## Run

With Docker running, set up and start Raven with one command:

```sh
./setup
```

The default setup opens browser-based setup automatically without terminal questions.
In that flow, name your workspace using the admin setup credential (`./dev login`
for local Docker), then create your personal profile and password. You become admin;
the browser flow stays locked until both steps finish. Operators can instead run
`./setup --workspace "Your team" --configure` to initialize a workspace without a
personal account. Slack recipients need no Raven account or prepared ownership map.
Optional browser accounts and invitations are described in
[user onboarding and repeatable tests](user-onboarding.md).
Sign in to Raven if needed, choose the repositories
Raven may read, and approve a one-time code on GitHub. No checkout path, token, or repository
URL needs to be pasted. Use `./setup --configure` for the optional agent MCP
client, Slack, and model configuration wizard. The web setup page also lets you
choose an agent, create its limited credential, and copy connection details into
the agent's MCP settings. To ingest local Git history as
well, use the explicit `./setup --repo /path/to/checkout` option.
The registered [Bridge Repository Access app](https://github.com/apps/bridge-repository-access)
is included by default; users never create an app. See [connection details](../docs/github-connection.md).
No hosted
connection service or distributed app private key is required.
It starts with an empty workspace; run `./setup --demo` to
add illustrative tasks and decisions. Press Enter to skip a connection. Later
runs preserve your data and settings. `./setup --yes` uses saved settings without
prompts. On Windows, use `.\setup.ps1` with Docker Desktop's WSL integration.

Agent clients connect to Raven's HTTP MCP URL using a limited agent token;
The web setup provides client cards: Cursor has a native install link, Claude
Code has a copy-ready command, and Codex has its own TOML configuration.
For browser-triggered **Start new session** / **Resume a session**, run the
opt-in local helper from the project folder:

```sh
./agents /path/to/your/project
```

It opens a paired browser tab. Keep the helper running. Only Claude Code and
Codex launch/resume actions are accepted, in that fixed project folder, with
normal permission prompts. The CLIs must already be installed and signed in.
macOS Terminal and Linux `x-terminal-emulator` are supported; Windows native
terminal launching is not yet supported. Resume opens the client's session picker,
not an arbitrary running terminal. Pairing is kept in tab memory; rerun the helper
after a reload or login that loses the pairing. A browser may request permission
to access the local launcher. Stop the helper with Ctrl-C when finished.

Agent credentials remain limited;
they do not need a launcher path into this checkout. Setup keeps that token in
private, Git-excluded client configuration. A loopback URL works on the local
machine; remote clients need a reachable HTTPS Raven deployment.

Open the printed URL. `./dev login` supplies the local operator's setup credential;
do not give that credential to a coding agent, which uses a separate limited token.
The stack creates all tables and search indexes. A checkout supplied with
`--repo` is indexed into the ownership graph; `--demo` adds a bundled sample
repository and decisions.
Data persists across restarts. See [DOCKER.md](../DOCKER.md) for configuration,
SQLite import, ingestion, testing, and backup/restore. `./dev down` stops it without
deleting data. The lightweight SQLite option below remains available.

Requires Python 3.10 or newer. The inbox, the MCP server, ingestion, and the deterministic resolution ladder have no third-party dependencies. The browser-based GitHub App connection additionally requires `cryptography` on non-Docker installs (Docker includes it). The model-backed rungs use your own Anthropic key or the claude CLI (see below). Managed execution is optional and uses the pinned dependencies in `requirements-agents.txt`.

```sh
python3 -m bridge --demo --port 7333
```

Open **http://127.0.0.1:7333**. `--demo` adds clearly labeled illustrative runs and owners only if the database is empty. Answers you enter are real, persistent writes to the local database. The demo does not run coding agents.

For a fresh workspace, choose another database:

```sh
python3 -m bridge --db .bridge/work.db --port 7333
```

The default database is `.bridge/bridge.db` in this checkout. Stop with Ctrl-C. Restart using the same database to retain work. The server binds only to `127.0.0.1`; it is not a hosted or multi-user deployment.

## Build the ownership map from a repository

```sh
python3 -m bridge ingest /path/to/checkout --db .bridge/work.db
```

Ingestion reads a local git checkout, on this machine only: authors and per-path touch counts, blame shares (all-time and recency-weighted with a 90-day half-life), CODEOWNERS entries, Reviewed-by trailers, merge commits, squashed PRs, and commits with a real body as records. Bots are dropped from ownership signals. Re-running refreshes the map; a material change in a share closes the old ownership row and opens a new one so history stays auditable. The same command is available as the `bridge_ingest_repo` MCP tool, as `POST /api/ingest`, and from **People & ownership** in the inbox. The repository's name (the last path segment) scopes the graph; a run whose repository is `acme/platform` uses the graph ingested from a checkout named `platform`.

### Keep it current from GitHub

A squash merge carries no trailer, a CODEOWNERS line often names a team, and the pull request said more than its commit. With a token that can read the repository, its pull requests and the organization's teams:

```sh
GITHUB_TOKEN=ghp_... python3 -m bridge sync acme/platform --db .bridge/work.db
```

The sync brings in every merged pull request updated since its polling watermark less a day of overlap (the watermark is the newest update a complete poll covered, so the second run fetches little; the overlap and idempotent writes mean a pull request updated at the watermark's own second is never skipped, and a webhook about one pull request never moves the watermark, because one object is not evidence that everything earlier was processed): the reviews that approved it and the person who merged it land as approval signals on the commit that merged (`approved-by`, `merged-by`, on the same rows git trailers land on), its files, its description and the substantive review summaries become records, and the members of every team CODEOWNERS names become people (a login nobody verified becomes a person with source `github`; a member who left is dropped, and the event says who). What GitHub said is kept in its own tables and laid on top of every git rebuild, so a re-ingest loses nothing. A failed sync records the error, keeps the cursor, and is repeated. On the server, `GITHUB_TOKEN` with `BRIDGE_GITHUB_REPOS=acme/platform,acme/ledger` syncs every listed repository every `BRIDGE_GITHUB_SYNC_MINUTES` (default 15); a GitHub webhook at `<public-url>/webhooks/github` (secret in `GITHUB_WEBHOOK_SECRET`; events `pull_request`, `pull_request_review`, `membership`, `team`) syncs a merged pull request the moment it lands. `GET /api/sync` shows the state per repository, `POST /api/sync` with `repo` runs one. When the sync is more than three days old, or a hosted repository was never synced, every route says so in its evidence.

## The resolution ladder

Every judgment request runs through the ladder before anything reaches a person:

1. **Memory.** Signed answers and evidence-resolved decisions, matched by hashed-embedding cosine and stemmed term overlap, recency-decayed, newest wins when two live answers to the same decision disagree, a retrospective question (who decided, what was the reason) keeps the decision it asks about, and a signed answer contradicted by a settled record on a concrete figure is escalated rather than served. A human may declare required facts, excluded facts, repository paths, and an expiry when answering. A later request must state every required fact and match the declared path; missing or contrary facts leave the prior answer as context for a person, not an answer to reuse. Legacy answers without a declaration still require fresh sign-off. An answer is never applied to the other side of a condition it does not mention: a follow-up about a month-to-month contract does not reuse the rule for annual ones.
2. **Ownership graph.** Questions about the ownership data itself, and accountability questions (who owns X, whose call is it, who signs off) resolve from blame, CODEOWNERS, and review signals reconciled per engineer. A live blame majority outranks a stale CODEOWNERS line, and the evidence says so and names who still holds CODEOWNERS approval.
3. **Records.** Merged PRs, commits, and tickets, by verbatim reference first, then the IDF lexical rung with focus and novelty gates. A newer record that announces it reverses the chosen one replaces it; a present-tense question checks for a newer ticket contesting the record; an open ticket is a proposal, never a settled decision. When no model is available to verify answerability, the selected record is cited as context for a person rather than its body being presented as the answer.
4. **Model-backed rungs** (with a backend): a selector arbitrates between memory and records or picks from a wide candidate sweep fed by query expansion; a grounded composer turns one record into a direct answer and refuses when the record does not answer; a joint composer answers multi-hop questions across records and memories; follow-up questions get their back-references resolved before retrieval.
5. **Assumption.** Low-stakes categories (rollout, compat, ux) get a default that follows an indexed precedent, flagged as a prediction to confirm.
6. **Human.** A question that duplicates a pending one returns that pending decision (never asked twice). Otherwise it routes to the owner the graph names, or to the inbox's configured path patterns when the graph has no signal.

Every outcome carries a `kind`: **evidence** (resolved or partial, with the citation in `evidence`), **prediction** (an assumed default, an unratified proposal, an unsigned earlier answer, or a signed answer given for another scope, none of which is sign-off), or **new** (pending with the accountable owner). Owners confirm or correct any of them in the inbox; a correction supersedes and the old answer stays in the revision history.

### Who may act on a decision

Authentication says who is asking; it does not say who may decide. One check (`bridge/authz.py`) runs before every answer, signature, correction, hand-on, assignment and rule, on every transport (the inbox, the REST API, Slack, managed execution), and it compares stable person ids, never display names:

| Standing | May answer, sign, correct, make a rule | May assign or hand on |
| --- | --- | --- |
| The decision's assigned owner | yes | yes |
| A required signer (an `approves` row for its scope) | yes | yes |
| Someone the authority map says decides or approves its scope | yes | yes |
| The coordinator | no | yes |
| An admin | only with `override: true`, recorded as an override | yes |
| Any other member, or a viewer | no | no |
| An agent credential | no | no |

Who actually acted, and on what standing, is recorded beside the assigned owner (`actor_name`, `actor_basis`), so a decision answered under an override reads as one. On loopback with auth off the single operator acts for the named owner, as before.

### The decision contract

Evidence is not authorization. A person stands behind a decision when the inbox recorded their answer, when they signed it, or when a reusable rule covers it (`authorized`). Everything else with an answer, however good its citation, still waits on a person (`blocking`, and `approval_pending` on `bridge_get_decision`), and the agent is told to prepare on it, not ship on it. The contract, each clause an invariant in `tests/test_contract.py`:

- **A task finishes only when nobody is waited on.** `bridge_finish_task` is refused, naming the nodes, while any node is an open question, an unsigned answer, a decision put in doubt by a correction, or a duplicate of one of those. On a task Raven engaged it is also refused while the diff changes a file someone else decides and nothing on the task was put to them: the agent writes what the change settles there as a node for that person, or gives one line per file under `uncovered` on why it settles nothing, which is kept as its claim.
- **An unsigned answer is a prediction for everyone else.** What the agent settled, or what a record resolved, never resolves another question as evidence: it comes back as a prediction with its provenance, and it closes no open question. Only signed answers are memory that resolves, and those still want the new owner's sign-off. A signed answer given for another scope (another named customer, other files) is a prediction here, not evidence.
- **A signature covers the text it was given for.** Every signature carries the fingerprint of the answer it signed. Correcting the answer, or the agent re-settling it, drops the signatures that covered the old text: what was signed by two of three approvers is unsigned again, and the task cannot finish. Signing is one compare-and-write, so two signers, or a signer racing a correction, serialize.
- **A reply is bound to what the person was shown.** Every message carries the fingerprint of the answer or question it showed. A reply approving an answer that has since changed is refused, with what it now says, and the current state goes out as a fresh message. An answer in Slack is stated as one (`answer: … because …`), so "I'll look tomorrow" is never recorded as a decision. An inbound event that cannot be applied is kept with its error and applied once when it is retried.
- **Only a rule resolves without a fresh signature, and only when the organization turns that on.** Every decision is request-specific by default, and automatic rule authorization is off (`auto_rules`) until a team enables it. An owner can declare a signed answer a reusable rule (`POST /api/decisions/:id/rule`, **Make this answer a reusable rule** in the inbox, `rule if <words> until <date>` in the Slack thread) with conditions and an expiry. A condition is a fact the agent stated (`plan=enterprise`, passed as `facts` on the node or once for the task) or a phrase the question carries and does not deny; a missing fact, a contradicted fact, or a phrase the question negates means a person decides. A rule covers its own scope unless its owner says it applies anywhere. Changing or ending it, its expiry (including the source answer’s declared applicability expiry), or a correction or supersession takes the authorization back from the outstanding work it covered: those nodes come back as needs-review, and their tasks cannot finish. A decision a rule authorized never becomes memory in its own right, so a rule's reach is its conditions and nothing more.
- **A decision is its question plus its scope.** On one tree, the same question with the same context and paths is one node, and a bare retry returns it; the same words about another named customer or other files, or under a new `client_ref`, is another node, linked as related. Across trees, a duplicate needs a compatible scope; the same question in another scope is related, never merged.
- **A duplicate reads through.** A duplicate node shows its canonical decision's answer, signer and next step, counts as waiting while the canonical waits, and stops waiting when it is answered.
- **A correction reaches every derived answer.** Every answer taken from another decision carries `source_id` and the source's revision. Correcting the source withdraws pending suggestions and marks answered and signed dependents (and their tasks) `needs_review`, transitively, until a person confirms or corrects them.
- **A sign-in is an exact identity.** GitHub sign-in binds to GitHub's immutable user id, to the exact login an admin recorded, or once to a verified email for a person with no login on record. A display name, an alias, or a login that merely looks like someone's name never selects an existing person or inherits their role; a renamed account follows its id, and that login on a new account is a stranger. Organization membership is membership, not identity.
- **A repository is owner and name.** `acme/platform` and `other-company/platform` never share a graph or a memory. A hosted identity maps onto a graph ingested from a bare checkout name only when that is unambiguous.
- **A signature is bound to what it covers.** Signing and answering over HTTP name the revision reviewed (`expected_updated_at`); a decision that moved on is refused, and the signed revision and answer fingerprint are recorded.
- **A kickoff key names one task.** `bridge_start_task` with a `client_key` returns the task it already started on a retry, and racing kickoffs with one key make one task.
- **Actionable work is never hidden.** The inbox payload carries every decision that waits on a person however old, counts come from the database (`counts`), and `GET /api/inbox` pages the needs-you queue oldest first.
- **Every required approver signs.** Where the authority map says several people must approve a scope, the node names them (`required_signers`); an answer by one of them, or a signature, is recorded as theirs and the node stays evidence until the last of them has signed. A prediction of how the owner will decide, composed from that owner's own signed answers, travels with an open question as a prediction (`How X has decided before`), never as sign-off.

## Model keys

For a multi-workspace Anthropic key, explicitly set the non-secret
`ANTHROPIC_WORKSPACE_ID` in the runtime environment (or the ignored local `.env`
used by Compose). The Anthropic section of `./setup --configure` offers this
optional ID after the API key; Enter keeps the saved ID or leaves it unset.
Raven sends it as `anthropic-workspace-id` on Messages requests;
unset or blank preserves existing single-workspace behavior. The ID is read at
call time and must be a printable header value of at most 256 characters. Raven
does not choose, discover or grant access to a workspace. See the
[Anthropic workspace-selection documentation](https://platform.claude.com/docs/en/manage-claude/authentication#select-a-workspace).
This setting covers Raven's direct Messages API client; external coding hosts
must configure their own supported workspace header separately.

The model-backed rungs read `ANTHROPIC_API_KEY` from the environment at call time and talk to the Anthropic Messages API with the standard library. The terminal setup wizard can save it in the ignored, owner-only `.env` file; do not commit or share that file. The model adapter does not put the key in decision records or logs. `BRIDGE_MODEL` selects the model. A direct local Python installation can use an installed, signed-in Claude CLI (`BRIDGE_MODEL_API=claude-cli`; `BRIDGE_CLAUDE_BIN` names the binary). Docker does not inherit that host login and normally uses `BRIDGE_MODEL_API=anthropic` with an API key. With no configured backend, or with `BRIDGE_SEMANTIC=0`, the ladder runs its deterministic rungs only: memory, ownership graph, records by reference and lexical match, dedupe, and routing. Questions those rungs cannot settle route to a person; natural-language interpretation needs inference, while explicit reply commands remain available. `BRIDGE_MODEL_API=none` disables backends outright.

Embeddings are always local (a hashed bag of stemmed words); no embedding API is called.

## What works

- The canvas: an agent kicks a task off with `bridge_start_task` and gets `engage` or `pass` with the reason; every decision it discovers is a node under the one it grew from; a node comes back resolved with a citation (marked for sign-off), predicted, pending with the owner the signals name, unrouted, or duplicate; what the agent settles itself goes on the tree for sign-off; `bridge_get_tree` reads it all back and `bridge_finish_task` is refused while a node waits on a person.
- Judgment inbox: **Needs you** lists every node waiting on a person, open questions and sign-offs alike. Answer a node with its rationale, sign off or correct what Raven or the agent resolved, add the follow-up questions the answer raises, assign or reassign owners, and mark which earlier decision an answer supersedes. **New request** starts a task on the canvas and writes its first node.
- Tasks: each task with its kickoff verdict and its tree, one level per rule; a task cannot finish while a node is pending. Recording an answer makes it available to the agent; it does not execute code.
- Ownership: an ownership graph ingested from Git history, blame, CODEOWNERS, reviews and imported record authors, linked to Slack contacts. Answers and referrals teach scoped first contacts; explicit ownership overrides remain optional.
- Decision memory: search signed and evidence-resolved answers by stemmed lexical overlap, hashed-embedding cosine, and backend-native text search (SQLite FTS5 or PostgreSQL), over question, context, answer, and rationale, recency-weighted, with superseded rows excluded and the newest of a same-subject pair ranked first.
- The ladder: applicable prior answers and model-checked records can resolve with a citation; without answerability checking, retrieved records remain context for a person. Predictions are labeled as such and never sign-off; corrections withdraw dependent suggestions and supersede.
- MCP: thirteen tools for tasks, source import, connection status and evidence export over the standard HTTP transport share the same data with the web inbox. None of them approves anything.
- Visibility: activity feed, live inbox refresh, per-decision event history including every rung's verdict, the ownership graph, and full JSON history export.
- Local protections: Host/Origin validation, CSRF tokens for REST writes, HTML escaping, a restrictive content security policy, and optimistic concurrency checks for inbox answers.

## Connect an agent

Open **Connections & setup** in the app and copy the generated configuration. Local and shared workspaces both use Raven's standard Streamable HTTP endpoint:

```json
{"mcpServers":{"bridge":{"type":"http","url":"http://localhost:7333/mcp"}}}
```

The transport uses the [MCP Streamable HTTP specification](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http), protocol version `2025-06-18`. Set `ANTHROPIC_API_KEY` in Raven's environment if you want the model-backed rungs. The server's instructions teach the agent the protocol below; the same page in the app walks through it.

Connected is not the same as used. On a real task, Claude Code held Raven's tools and instructions and never called one. Tell the agent in the file it reads for every task (`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex and Cursor):

```markdown
## Raven

This team uses Raven (the `bridge` MCP server) to route the judgment calls in a task to the person who owns them. At the start of every task, before reading or editing anything, call `bridge_start_task`, then follow what Raven returns for the rest of the task.
```

## Share one Raven with a team

One Raven process that everyone's agents and browsers reach, one memory, one map. Off loopback, auth is on: every request carries an identity, roles gate what it may do (viewer reads; member answers, signs, kicks off tasks and writes nodes; admin also maintains people, authority, tokens, ingestion and settings), and every answer, signature, follow-up and kickoff is attributed to the person who made it, whatever the payload says.

```sh
BRIDGE_ADMIN_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') \
python3 -m bridge --host 0.0.0.0 --port 7333 --public-url https://bridge.acme.internal --db /srv/bridge/bridge.db
```

Put TLS in front of it (a reverse proxy); `--public-url` is the address people use, and Host and Origin are checked against it. An agent's `bridge_wait` over HTTP can send progress notices every 15 seconds, with each call bounded by `BRIDGE_MCP_CALL_BUDGET` (50 seconds by default). The host calls again with `since` to continue waiting. The proxy must not buffer `text/event-stream` responses (Raven sets `X-Accel-Buffering: no` for nginx), and its read timeout must exceed the progress interval. Two ways in:

- **GitHub sign-in.** Set `BRIDGE_GITHUB_CLIENT_ID` and `BRIDGE_GITHUB_CLIENT_SECRET` (an OAuth app whose callback is `<public-url>/auth/github/callback`), optionally `BRIDGE_GITHUB_ORG` to require membership. A GitHub login or verified email is matched to a person under **People & ownership**; the first person ever admitted is the admin; later strangers are refused unless `BRIDGE_AUTH_ALLOW_SIGNUP=1`.
- **Tokens, of two kinds.** The bootstrap admin token (`BRIDGE_ADMIN_TOKEN`) adds people and mints their first token (`POST /api/tokens`); after that everyone mints their own under **Connections & setup**. An **agent credential** (the default, `kind: "agent"`) is what a coding agent carries: it kicks off tasks, writes nodes and reads, as that person, and it cannot answer, sign, correct, hand on, make a rule, mint a token or become a browser session. A **human token** (`kind: "human"`) is for a person's own scripts and carries their role. Both are shown once and stored hashed. Sessions are signed cookies (`BRIDGE_SECRET`, else a secret kept in the database).

An agent on a laptop connects directly to the shared Raven; the config under **Connections & setup** includes its limited credential:

```json
{"mcpServers":{"bridge":{"type":"http","url":"https://bridge.acme.internal/mcp","headers":{"Authorization":"Bearer brg_..."}}}}
```

Every tool call goes to `POST /mcp` with the token; the person behind the token is the requester of every task and node it writes, and the token's label is the agent's name. `BRIDGE_AUTH=off` keeps the local single-operator mode on loopback, where no token is required.

### Reaching the person in Slack

People do not open an inbox to find out a decision is waiting on them; Raven brings it to them. With a Slack bot token in the environment every notification a decision produces goes out as a message: a question routed to its owner (`ask`), an answer waiting on a signature (`signoff`), an answer that leaned on something since corrected (`review`), a hand-on (`reassigned`) and the answer to a question someone asked (`answered`). Slack directory sync discovers people and links their member IDs automatically; their DMs are used when reachable; otherwise the message goes to the fallback channel with a note naming them, and with neither the notification is kept as failed under **Deliveries** so nobody wonders why the owner never replied.

```sh
SLACK_BOT_TOKEN=xoxb-... SLACK_SIGNING_SECRET=... SLACK_FALLBACK_CHANNEL=C0123456789 \
python3 -m bridge --host 0.0.0.0 --port 7333 --public-url https://bridge.acme.internal --db /srv/bridge/bridge.db
```

Use all required bot scopes from the supplied [manifest and setup guide](slack.md): `chat:write`, `im:write`, `im:history`, `app_mentions:read`, `channels:history`, `users:read`, `users:read.email`, `assistant:write` and `search:read.public`. The last two enable supported public-channel search. `SLACK_API_BASE` (default `https://slack.com/api`) points Raven at a Slack-compatible Web API instead, such as an egress proxy or an explicitly simulated test service. Subscribe to `message.im`, `message.channels` and `app_mention` at `<public-url>/webhooks/slack`; that URL must be reachable by Slack over HTTPS. The endpoint checks the signature and connected workspace. The fallback channel can also be set from the inbox (`slack_fallback_channel` under settings).

People reply in the thread Raven started. With inference enabled, they can ask for context, give or amend an answer, or refer the question in ordinary language. Raven reads the proposed action back for confirmation; a casual acknowledgement such as “OK” is not signoff. Explicit command shortcuts remain available:

- an answer, with `because …` for the rationale: recorded as that person's signed answer, at the revision the message was sent for, so a decision that moved on since is refused rather than overwritten;
- `sign off` (or `approve`, `lgtm`, `confirm`) on an answer Raven or the agent put on the table: signs it; a different answer in the reply corrects and signs it;
- `not me @person`: hands the decision on and explains why to the next person. The conversation teaches a scoped first-contact signal; it does not grant broad business authority. Add `just this one` to avoid learning a reusable route. Explicit ownership/approval assignments are separate controls.

A newly joined member or referral target is verified through Slack’s API and added as a contact automatically. No Raven account is required. Bots, guests, deactivated users and users outside the connected workspace are not imported. Every message is queued with a dedupe key (decision, kind, revision, person), retried with backoff, and listed with its state under **Deliveries** (`GET /api/deliveries`, `POST /api/deliveries/:id/retry`). The same hand-on is available from the inbox (**Hand on & learn**) and over `POST /api/decisions/:id/refer`. A question open longer than `overdue_hours` (a setting, 72 by default) gets its owner a reminder once a day, and past twice the window the coordinator hears too; the inbox lists them under **Overdue**. A decision written in a channel as `record: <what was decided>` (or `@Raven record: …`) becomes a record with its permalink as locator, under the repository named in `slack_capture_repo`: evidence, not sign-off.

**Microsoft Teams** takes the same outbox through an incoming webhook (`TEAMS_WEBHOOK_URL`, used when no Slack token is set): every message goes to that one channel, addressed to the person, with the link to the inbox where they answer; Teams webhooks carry no replies.

#### The task page a message links to

A direct message to a person carries their own link, `<public-url>/brief#rvn_…`. The token rides in the fragment, which browsers never send, so it stays out of server logs and Referer headers; the page sends it as the `X-Raven-Link` header. Only its hash is stored (`brief_links`). A link names one person, one task and the decision the message was about, and expires after `brief_link_days` (14 by default, at most 90). It is never put in a channel or fallback post: it acts as its person, so it goes only where they read it. A link that stopped working can ask for a new one, which goes to its person's DM, never to whoever holds the old link (one per old link an hour, three a day).

The page shows the decision waiting on the person (its brief, the agent's context, what Raven found, why it came to them, and a prediction labelled as a guess), the request and who made it, every decision on the task with who signed or still owes it, everyone contacted and where each question stands for them, the notes, and the history. From it the person answers, signs or corrects (`POST /api/brief/answer`, with the revision they read), hands the decision on (`/api/brief/refer`), adds a note (`/api/brief/note`), and withdraws a note they added (`/api/brief/withdraw`). Every action runs the same permission check as the inbox and Slack, acting as a member: an administrator's link carries no override, and a viewer's link only reads. A hand-on from a link is for that question only and teaches Raven no route, because a forwarded link would otherwise decide routing for a whole directory. A signed-in member opens the same page from the task overview (**Open task page**, `POST /api/tasks/:id/link`), which keeps one such link per person and task.

`brief_mode` (People & ownership → **Task page in messages**) is `off` (messages link to the inbox as before) or `static` (the default: the page described here, which needs no model). A conversation with an agent on this page is planned as a follow-up.

A person can create a login from their link (`/api/brief/account`): the account is the person the map already names, with the role it gives them. Administrators are refused; they sign in with GitHub or an invitation. Set `brief_signup` to `0` to require invitations for everyone.

### The canvas protocol

Raven is the canvas for the decisions inside a task. The host agent breaks the task down; Raven never does. Raven finds out who owns what, what the org already settled, and who has to be asked, and it writes that back onto the same tree the agent writes to. `--demo` seeds one such task so a first run shows a kickoff verdict, a tree, and a node the agent settled waiting for sign-off under **Needs you**.

1. **Kickoff.** The agent calls `bridge_start_task` the moment a task is kicked off, before any work, with the task as given, the repository, who kicked it off (`requester`) and the paths it expects to touch. Raven looks at the task with what it knows on its own (the tree, MAINTAINERS and CODEOWNERS, git history and blame, prior decisions, what is already pending) and answers `engage` or `pass` with the reason and a discovery digest. Most tasks are trivial and get `pass`: Raven engages only for a reason (the org already decided something close, a decision is pending on the same paths, the change spans areas owned by different people, the task speaks of a change that is a call for its owners, or the task itself poses a question).
2. **Nodes.** On `engage`, every decision the agent discovers becomes a node with `bridge_add_node`, under the node it grew from (`parent_id`). The tree is not known upfront: level n+1 depends on the answers at level n. Each node runs through the ladder and comes back `resolved` (from records or signed answers, with a citation, marked for sign-off by the person the signals name), `predicted` (an unconfirmed default from precedent), `pending` (routed to the person the signals name, never the requester), `unrouted` (Raven does not know, and says why) or `duplicate`. Writes are idempotent by `client_ref` and by question, so a retry never asks twice. A node with no paths of its own inherits its parent's, then the task's.
3. **What the agent settles itself** goes on the tree with `bridge_settle_node`, marked for sign-off, so the people who own the area see the whole decision record and can correct it.
4. **What people add.** In the inbox a person answers a node, signs off a node Raven or the agent resolved (or corrects it, which is a signed answer), and writes the follow-up questions the answer raises: the right questions for level n+1. Those land as `suggested` children of the node; the agent adopts them with `bridge_add_node(adopt=...)`. A follow-up is optional unless the person marks it required: then the task cannot finish until the agent adopts it and it is answered. Until someone signs, the node counts under **Needs you** like an open question.
5. **Reading the tree, and waiting.** `bridge_get_tree` returns the verdict, every node nested under its parent with its status and answer, the follow-ups people added, the nodes written before their parent was answered (their question may be wrong now) and what to do next. The agent reads it before an irreversible step. When a node waits on a person, the agent does its independent work and then calls `bridge_wait`, which returns as soon as a person acts (or at the timeout, naming who is still being waited on); people answer in Slack or the inbox minutes or hours later, and an answer that lands while the agent is not waiting is on the tree whenever it reads it. If the agent's session restarts, `bridge_start_task` with the same `client_key` returns the same task. `bridge_finish_task` closes the task and is refused while a node still waits on a person, or while a follow-up a person marked required is not adopted.

6. **Exact change proof and advisory review.** Send the complete UTF-8 patch, including staged, unstaged and new files, to `bridge_finish_task` without trimming or changing its final newline. The optional `diff_sha256` is computed by the host from the original patch bytes; Raven rejects a mismatch before completing the task. It detects transport damage, not independent repository or test attestation. A task can be authorized and completed while its model review is still `running`. A whole-task `bridge_wait` also waits for that review and returns its current result within the same bounded call budget. When it finishes, repeat `bridge_finish_task` with the same exact diff and checks to save the terminal review, then export. `bridge_export_proof` never rewrites a saved bundle: `review` reports the current reading, `review_pending` identifies running work, and `review_snapshot_current` says whether the immutable bundle contains that reading. Follow its `next` step before attaching an outdated or failed review snapshot.

Read acknowledgments are task-wide shared awareness, not proof that each individual agent or host session has read an answer. The current protocol does not distinguish sessions sharing one task. Every host should read the current tree before relying on decisions. The unread-answer check applies to MCP and authenticated agent REST completion (including the legacy run-status route); a full agent REST tree read acknowledges the same revisions. Human/operator REST completion does not assert that an agent read the answers. Full-tree responses include an `observed_revision` receipt captured before decision rows are read; `observed_at` remains a compatibility cursor. A whole-task wait acknowledges only when its returned full changes cover every unread decision in that receipt. Filtered waits and truncated resume summaries do not acknowledge the task. Receipts use the count and greatest ID of committed, append-only human events per decision, so tied timestamps and late lower-ID commits remain detectable. SQLite-to-PostgreSQL import resets receipts and requires a fresh read.

State lives in the shared database (PostgreSQL in Docker, optionally SQLite for direct Python installations); it does not depend on the MCP process staying alive. The same protocol is available over REST (`/api/tasks/start`, `/api/tasks/:id/nodes`, `/api/tasks/:id/settle`, `/api/tasks/:id/tree`, `/api/tasks/:id/finish`, `/api/decisions/:id/followups`, `/api/decisions/:id/signoff`).

**Who to ask** has two layers. What the organization verified comes first: the people table (name, email, GitHub login, Slack id, aliases), teams, and the authority map, which says who *knows*, *decides* or *must approve* for a path pattern, a decision category (billing, pricing, security, auth, privacy, compat, rollout, data, infra, legal, customer, ux, ops) or a whole repository, with a source, who asserted it and an optional end date. A person the map names decides the scope whatever git says, is routed to even when they are the requester (the decision is theirs), and is never dropped for inactivity; a team named in CODEOWNERS becomes its members once the team is known; an unaccepted referral is a candidate, not the owner. A configured owner (**People & ownership**, `POST /api/owners`) is a person with verified authority for their path patterns. Below that layer, the repository itself: MAINTAINERS and CODEOWNERS where they exist (a team there is its synced members), who reviewed, accepted or merged the recent changes under the file the question names, its directory and the directory above (the nearer the louder; pull request approvals count once GitHub is synced), git blame of the named files, and who usually approves the requester's own changes. The requester is never routed to on inferred signals; a bare first name or an alias resolves to the one person it names. Every route carries the lines that justify it, and a verified route starts with `verified:`.

Without explicit authority, Raven infers a first contact from connected sources and prior scoped conversations. A missing reachable contact goes to the configured Slack triage channel or optional **coordinator**, otherwise it remains visibly unrouted. Claiming or referring a triage question assigns it; it does not approve the answer. **Pilot mode** (`require_verified_route`) can require explicit routing authority for teams that want it. `GET /api/people` lists people, teams, authority and settings; `POST /api/people`, `/api/teams`, `/api/authority` and `/api/authority/:id/end` maintain optional overrides; `bridge_list_owners` shows the agent these signals.

If a relevant learned route depends on material facts missing from the current task, `bridge_add_node` can return `needs_scope_clarification` instead of sending a misleading DM. The tree retains `scope_clarifications`, and finish waits for them. Establish the current facts from the requester or current sources, then retry with the same `client_ref` and explicit facts. A previous decision's facts are context to inspect, never facts to silently copy into a new customer or release.

**With inference configured**, a fast model (`BRIDGE_FAST_MODEL`) can map a question with no path to the tree's directories, write the brief the owner reads and advise the kickoff verdict from the same digest the rules saw. A prior decision or pending question on the paths still engages. Without inference the deterministic protocol remains available.

The thirteen tools exposed to agents (the running server's `tools/list` is the authoritative schema):

| Tool | Purpose |
| --- | --- |
| `bridge_start_task` | Start with `title`, `repo`, the original `goal`, optional `agent`, `requester`, discovered `paths`, `client_key` and task `facts`. Returns `task_id`, a verdict (`engage`, `pass` or `unplaced`) and discovery. Resume an existing task with `task_id` alone. |
| `bridge_add_node` | One decision as a node: `task_id`, `question`, optional `context`, `parent_id`, `paths`, `options`, `category`, `client_ref`, `requester`, `adopt`, `depends_on`. Idempotent. |
| `bridge_settle_node` | The answer the agent settled a node with, and why; stays on the tree for sign-off. |
| `bridge_get_tree` | The whole canvas: verdict, nested nodes with status and answers, what each depends on, follow-ups people added, notes people wrote on the task, what to do next. |
| `bridge_wait` | Wait for the people: blocks up to `timeout` seconds until a person acts on the task (an answer, a sign-off, a correction, a hand-on, a follow-up question) and returns what changed; with `since` (the `observed_at` of the last read) what happened meanwhile comes back at once. |
| `bridge_finish_task` | Finish with the complete diff and claimed checks; refused while decisions, unread changes, required follow-ups or scope clarifications remain. Close an unused mistaken task with `status=abandoned` and `reason`. |
| `bridge_get_decision` | Retrieve one node as a decision record (`decision_id` is a `node_id`), including current answer, approval status, and history. |
| `bridge_search_decisions` | Search a `query` against signed and evidence-resolved decisions; returns sourced candidates. |
| `bridge_list_owners` | Configured owners plus the ownership graph, optionally by `repo`. |
| `bridge_ingest_repo` | Build or refresh the graph from a local checkout at `path`. |
| `bridge_import_record` | Import a ticket, document, Slack message or note the agent retrieved through its own connected tools (`repo`, `kind`, `ref`, optional `title`, `body`, `author`, `url`, `status`, `paths`, `created_at`) as a record the ladder can cite: evidence, never sign-off. |
| `bridge_connection_status` | What is connected and what is not: readiness findings, ingested sources and records, GitHub sync per repository with how to make it current, Slack contact discovery, failed deliveries and replies, and the triage channel. |
| `bridge_export_proof` | Export the saved finish bundle: submitted diff and SHA-256, signed decision revisions, scope, attribution, citations, host-reported checks and integrity/staleness indicators. A digest is not a human digital signature or proof that tests ran. |

A resolved node can be acted on with its citation once signed; a predicted node must be treated as unconfirmed; a pending node is waited on with `bridge_wait` while independent work continues. Every tool failure comes back as a result with `isError` under the request's id; a stdin line over 1 MiB is refused with a parse error. Tasks launched through the optional Agents API integration go through the same canvas: kickoff on submit, every `request_judgment` a node through the ladder, the answer delivered when a person answers or signs it. [PILOT.md](../PILOT.md) is the checklist for standing all of this up for one team, what to watch, and the recovery for each thing that goes wrong.

From the command line:

```sh
python3 -m bridge ask "What is the maximum lifetime of a service token?" --repo acme/platform --path auth/tokens.py --db .bridge/work.db
```

## Launch ordinary work through Raven

```sh
python3 -m venv .bridge/agents-venv
.bridge/agents-venv/bin/python -m pip install -r requirements-agents.txt
# Set OPENAI_API_KEY in this terminal, then:
.bridge/agents-venv/bin/python -m bridge --agents --port 7333 --db .bridge/work.db
```

Alternatively pass `--api-key-file .bridge/openai-api-key` to read a locally saved key. Add an owner with the path pattern `billing/*`, click **Start a task**, select **Disposable billing fixture**, and enter **Add usage-based pricing**. The backend kicks the task off on the canvas (verdict and discovery on the run), starts an isolated OpenAI-hosted workspace, and writes each of the agent's questions as a node through the ladder. Answer or sign in the inbox; the worker delivers the signed revision and the same task continues even with the browser closed. `--managed-repo NAME=/path/to/checkout` (repeatable) offers a local checkout besides the fixture: the files git tracks at its HEAD, text only, 256 KB per file and 8 MB in all. This optional hosted execution mode is separate from connecting an already-installed coding CLI over MCP.

## REST interface

Read `/api/state` to get the current state and `csrf_token`. Writes require `Content-Type: application/json` and `X-Bridge-CSRF: <token>`.

- `GET /api/decisions/:id`, `/api/search?q=...&repo=...`, `/api/pending?run_id=...&path=...`, `/api/ownership?repo=...&limit=...` (the ownership graph's live rows; `limit=0` for all), `/api/export`
- `POST /api/owners` with `name`, `team`, `patterns`
- `POST /api/runs` with `title`, optional `agent`, `repo`
- `POST /api/decisions` with `run_id`, `question`, `context`, optional `path`, `owner_id`, `category` (one question through the ladder on a registered run)
- `POST /api/ingest` with `path`, optional `repo`, `max_commits`
- `POST /api/tasks` with `task`, configured `repository`, and a stable `submission_key` (requires `--agents`)
- `GET /api/executions/:run_id/artifacts/:artifact_id` to download a file belonging to that run
- `POST /api/decisions/:id/assign` with `owner_id`
- `POST /api/decisions/:id/answer` with `answer`, `rationale`, optional `supersedes`, `expected_updated_at` (required for managed decisions)
- `POST /api/decisions/:id/followups` with `questions` (a list, or one per line), `by`, and optional `required` (the task cannot finish until the agent adopts them and they are answered); `POST /api/decisions/:id/signoff` with `by`, `expected_updated_at`, optional `answer` and `rationale` to correct
- `POST /api/decisions/:id/refer` with `person`, `by`, optional `role` (`knows`, `decides`, `approves`), `scope_kind` and `scope`, `note`: hand the decision on and record the referral as authority to be accepted on answer
- `POST /api/decisions/:id/rule` with `by`, `expected_updated_at`, optional `conditions` (phrases, one per line or semicolon) and `expires` (a date); with `end: true` the rule stops
- `POST /api/tasks/start`, `POST /api/tasks/:id/nodes`, `POST /api/tasks/:id/settle`, `POST /api/tasks/:id/finish`, `GET /api/tasks/:id/tree` (the canvas protocol, same fields as the tools; `nodes` also takes an explicit `owner_id`, which is how the inbox's **New request** form starts a task and writes its first node)
- `POST /api/runs/:id/status` with `status`
- `GET /api/inbox` (every blocking decision, never truncated, with counts), `GET /api/people` (people, teams, authority, settings), `POST /api/people`, `POST /api/teams`, `POST /api/authority`, `POST /api/authority/:id/end`, `POST /api/settings` with `coordinator`, `require_verified_route`, `slack_fallback_channel`, optional `repo`
- `GET /api/me`, `GET /api/tokens`, `POST /api/tokens` with `label`, optional `person_id` (admin), `POST /api/tokens/:id/revoke`; `POST /mcp` is the standard MCP transport, with the caller as requester
- `GET /api/deliveries?state=...`, `POST /api/deliveries/:id/retry`; `POST /webhooks/slack` (Slack Events API, verified by signature, no session)
- `POST /api/deliveries/inbound/:event_id/retry` (admin; apply a Slack reply that failed), listed by `GET /api/deliveries` as `inbound_failed`
- `GET /api/sync` (the GitHub sync state per repository), `POST /api/sync` with `repo` (admin; runs one sync, or registers the repository when the server has no token); `POST /webhooks/github` (verified by `X-Hub-Signature-256`, no session)
- `POST /api/tasks/:id/notes` with `text` and `by` (context a person adds to a running task; the agent reads it in `bridge_get_tree` and `bridge_wait` returns it), `GET /api/tasks/:id/trace` (every event, notification and node standing of a task, in order)
- `POST /api/records` with `repo`, `kind` (`ticket`, `doc`, `slack`, `note`), `ref`, `title`, `body`, optional `author`, `url` (the immutable locator), `created_at`, `paths`, `status`: a record from outside git, evidence the ladder cites, never sign-off; `GET /api/inbox?overdue=1` (open questions older than `overdue_hours`, a setting alongside `slack_capture_repo`)

## What is enforced, and what is guidance

`bridge_finish_task` is refused while a decision waits on a person, and every authorization is recorded with who gave it and on what standing. Raven does not sit between the agent and the repository: nothing stops an agent from editing, committing or deploying without asking. The protocol is guidance the agent follows and the finish gate is a claim to check, not a control on shipping; keep code review as the release gate until a host or CI integration enforces it.

`bridge_finish_task` says so in its own answer: it returns `verified: false`, lists who authorized what, records the `checks` the agent says it ran as a claim, and carries a caveat naming the difference. Raven gates **authorization**, not **conformance**, and the difference is not theoretical. In `evals/real_oss`, on a real repository with a real agent, an owner corrected an answer on the canvas, the agent read the correction, reported that its change matched it, and finished the task with the gate satisfied; the diff did not match. A signature says a person decided, not that the code does what they decided. Read the diff.

Given the agent's diff, a model reads each signed answer against it and reports `follows`, `departs` or `unclear`, in two passes. The first breaks the answer into requirements and quotes the line of the diff each is judged by; a requirement read as met with no such line in the diff is not met. The second tries to break every requirement read as met, walking the early exits and boundary values around the quoted line, and a counterexample through the change's own code turns the reading `unclear` and is returned beside the requirement. What a reading depends on that the diff does not show is listed as `unexamined`. That is still a model reading the diff the agent supplied, not a test: it can miss a path, and it says nothing about the code, tests or docs the answers do not cover. On eb9d22d a patch read as following "no further retry once the budget is spent" while an unchanged early return let one more attempt through; in one live run all four answers read as followed while the agent's new user guide said same-origin redirects forward every header unchanged, which is false for a 303. Independent tests and code review stay the gate. The reading runs in the background and is kept: a finish that cannot wait for it answers with `review.status` running, the result appears on `bridge_get_tree` under `review`, and a retry with the same diff reads it back instead of reading again. The finish also lists, under `uncovered`, files the diff changes whose decider was asked nothing on the task, with the agent's reason where it gave one. A kept reading is about the signed answers it read: when one changes afterwards, the tree marks the review `stale`, names the decisions, and the next finish with the current diff reads it again.

## Before you trust it: what is not set up yet

`GET /api/state` carries `readiness`, and **People & ownership** shows what is missing and what to do. `bridge_connection_status` gives the host the same practical connection checks. Repository history supplies candidate contacts, not guaranteed business deciders, and CODEOWNERS often names teams. Connect Slack's member directory and allow referrals rather than treating a team handle as a person. Explicit authority overrides are optional; unresolved contacts and failed deliveries must remain visible.

It reads authority where it applies: per repository that has been ingested, and only rows that are accepted, still in force today, and say the person decides or approves rather than merely knows. A row recorded for another repository or one whose `effective_to` has passed used to clear the blocker while every question still fell through, which told an operator the map was set up when it was not.

`bridge_start_task` also returns `candidates`: decisions the task may contain, drawn from the task's own words, a prior decision that may no longer hold, and areas listed to different people. They are prompts for the agent, not decisions, and each says which signal it came from. Raven cannot route a decision nobody writes down; this is what it can do about that without pretending to have read the change.

The verdict is read from the task, not from what the host wrapped around it. A host's own workflow instructions arrive in the same `goal` field as the brief, and a Grafana task that Raven passed on engaged once boilerplate containing "authorization" and "release" was appended to it: true of the host, and silent about the work. A labelled block of house rules is set aside, and a sentence addressed to the agent that names nothing in the repository is not read as a statement about the change. What is left, the engage reason quotes, so the words it read can be checked against the task. `evals/real_oss_remote/probe_discovery.py` measures both halves.

## Scope and limitations

For Docker/PostgreSQL, use `./dev backup` and the restore workflow in [DOCKER.md](../DOCKER.md). For a direct SQLite installation, `python3 -m bridge backup /path/to/copy.db --db /path/to/bridge.db` takes a consistent online copy, restored by pointing `--db` at it. `GET /api/export` is a JSON view of decisions and runs, not a complete backup.

On loopback with auth off this is a **local, single-operator** workspace: the operator records answers on behalf of the named owner and local trusted processes share the database. Shared deployments require authentication and TLS; agents, people and task links have distinct permissions. Docker uses PostgreSQL, while direct Python can use SQLite. Keep a separate deployment/database per organization: this is not a multi-tenant service, and moving to PostgreSQL alone does not establish horizontal scalability.

Remaining limits include inbound Teams replies (its incoming webhook is outbound-only), native Jira/Linear/Notion/Confluence polling (records arrive through host connectors and `bridge_import_record` or `POST /api/records`), private-channel Slack search OAuth, automatic Slack archive backfill, multi-tenant hosting and enterprise-wide tracing. Automatic first-contact inference exists but cannot prove decision-making authority. Retrieval is lexical and hashed-embedding based; there is no learned embedding model. Similar prior decisions are evidence for their original context, never blanket approval for a new action.

The optional voice interview is a browser microphone/speech interface reached from Raven's task workflow, with guided prompts and model-backed follow-up when configured. It requires the participant to review and explicitly sign the resulting decision. It is not a native Slack call. Voice-provider, browser microphone and real-human checks must be reported separately from simulated/text interview tests.

## Verify

```sh
python3 -m unittest discover -s tests -v
PYTHONHASHSEED=0 python3 -m unittest discover -s tests
node --check web/app.js
```

`tests/test_trust.py` is the trust contract as rejecting tests: an agent credential and an unrelated person refused on every transport, a lookalike sign-in refused, signatures dropped when the text changes, a stale Slack approval refused, a rule that ended or expired taking its authorization back, a webhook that never moves the polling watermark, and a failed inbound reply applied once on retry. Each one is a probe from the first-user readiness review (internal evidence not included), asserted as the safe outcome.

The Python suite is offline and deterministic: the model-backed rungs are exercised through fakes, Slack through a fake transport. `tests/test_e2e_agent.py` runs the loop end to end with a scripted host agent speaking MCP (in process, as a stdio subprocess that is killed and restarted, and over HTTP against a shared Raven): kickoff once, the question in Slack, the late reply, the resumed wait, the settled change, the sign-off gate, a correction reaching the task that reused the answer, and an owner who cannot be reached. The browser suites require Playwright and an installed Chromium browser:

```sh
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser.cjs
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright node tests/browser-agents.cjs
```

Optionally set `BRIDGE_BROWSER` to an existing Chromium executable and `BRIDGE_PYTHON` to a Python executable. Record the versions you ran with (`node --version`, the Playwright package version, and the browser's `--version`) beside the run: the mobile layout assertions are the ones that move with a browser version. The suite starts an isolated server with a temporary database seeded by `--demo`, checks the seeded canvas task (its verdict, its tree, and the sign-off under **Needs you**), creates a request through the form, tests browser approval through independent MCP retrieval, and exercises corrections, export, escaping, setup copying, and mobile views. Screenshots are saved in `test-results/`; temporary test data is removed automatically.

The sealed 20-question hard test that measured the rebuild before the ladder port and the ported build after it lives in [`evals/hardtest`](../evals/hardtest/README.md), with the synthetic organization, the answer key, the runner, and the recorded results. [`evals/e2e`](../evals/e2e/README.md) evaluates the loop itself: the same tasks under three arms (the host alone, routing alone, Raven), scored on decisions discovered, interruptions, first-contact acceptance, the sign-off gate, adherence and lost answers, with the people simulated from an answer key; it runs on the example file in the test suite and on a pilot's own task file from the command line. `python3 -m bench.scale.measure` times one question against a memory of thousands of decisions, whole-scan and bounded ([SCALING.md](../SCALING.md) keeps the numbers). To test Raven end to end on a real open-source repository on your own machine, follow [docs/local-e2e-open-source.md](../docs/local-e2e-open-source.md). It covers which repositories have a production-like spread of owners, measured, and [evals/newdev/local-oss-e2e-prompt.md](../evals/newdev/local-oss-e2e-prompt.md) hands the whole run to a coding agent.

## The routing replay bench

`bench/routing` replays real git history to measure who-to-ask routing and the canvas: each sampled commit is hidden together with everything after it, turned into the decision it was making, and asked against the repository as of its parent; the answer is scored against the people git says approved it. `tree_exam.py` does the same for a whole merged series as one task on the canvas, kickoff verdict included. No API is used; labels come from git alone, or also from a Raven database that synced the repository from GitHub (`BRIDGE_BENCH_GITHUB_DB`), which is how a squash-merge repository gets its approvers labeled. Every run now scores three baselines beside Raven on the same items (the first human CODEOWNERS or MAINTAINERS lists, the most recent reviewer, a manual map from `BRIDGE_BENCH_MANUAL_MAP`), reports coverage, precision among routed, acceptable contact and authorized signer apart, and stratifies by team-only listings, missing trailers, aliases, new files and stale listings. The protocol, the datasets, the four conditions, the metrics and the recorded runs are in [`bench/routing/README.md`](../bench/routing/README.md); the full held-out run at head is the pilot team's to execute with the scratch directory, and it has not been run since the merge-weighting smoke.


### Model work and tool timeouts

Kickoff and node creation return a static reading immediately. When `model_pending` is true,
Raven continues the model reading in the background. `bridge_wait` or `bridge_get_tree` returns
its result. A human answer received in the meantime takes precedence. After an interrupted
read, the static result remains visible and the question is delivered for a person to answer.

MCP waits default to at most 50 seconds per call (`BRIDGE_MCP_CALL_BUDGET`). Call again with
`since` to continue waiting without losing an answer. Hosts may set `MCP_TOOL_TIMEOUT`, but
raising their timeout is optional. Finishing starts or reads a saved diff review and returns
within the same budget. The review checks the signed requirements, looks for counterexamples,
and checks for extra policy conditions or exemptions nobody authorized. It is a model reading,
not a test result or a new approval.

Resume a task by passing only `task_id` to `bridge_start_task`. A mistaken task nobody has
acted on can be closed with `bridge_finish_task(status="abandoned", reason="...")`.
