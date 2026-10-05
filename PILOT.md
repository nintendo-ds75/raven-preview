# Running a pilot with one team

What a pilot proves: an agent kicks a task off once, the decisions inside it reach the people who own them where those people already are, the answers come back to the same task while the agent waits, the change follows the answers, and nothing ships on an answer nobody signed. This is the checklist for standing that up for one team, what to watch while it runs, and what to do when something goes wrong. Every item names the command, the page or the API it uses.

## Before the first day

**One shared Bridge.** One process on one host, one SQLite file, TLS in front.

- [ ] Decide the one database path and use it in every command; the CLI's default is the checkout's `.bridge/bridge.db`, which is not the server's.
  ```sh
  export BRIDGE_DB=/srv/bridge/bridge.db
  ```
- [ ] Start it off loopback with auth on and the address people will use. The bootstrap token is written where the operator can read it back, not left in a shell that closes:
  ```sh
  umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > /srv/bridge/admin-token
  BRIDGE_ADMIN_TOKEN=$(cat /srv/bridge/admin-token) \
  python3 -m bridge --host 0.0.0.0 --port 7333 --public-url https://bridge.acme.internal --db "$BRIDGE_DB"
  ```
  Hand the token to the operator over the channel your team uses for secrets, and delete the file once the first admin has signed in with GitHub. It only ever mints tokens and adds people; it decides nothing (an admin acts on someone's decision by saying `override`, and the record says so).
- [ ] Back it up before the first day and on a schedule: `python3 -m bridge backup /srv/bridge/backups/$(date +%F).db --db "$BRIDGE_DB"` copies the whole database (people, authority, settings, tokens' hashes, integration state, every decision and revision) with SQLite's online backup, safe while the server runs. `GET /api/export` is a JSON view of decisions and runs, not a backup. Restore by pointing `--db` at the copy, and rehearse it once: start a second Bridge on the copy and check that the inbox lists the same decisions and a task can still be answered.
- [ ] GitHub sign-in: an OAuth app whose callback is `<public-url>/auth/github/callback`, its id and secret in `BRIDGE_GITHUB_CLIENT_ID` and `BRIDGE_GITHUB_CLIENT_SECRET`, `BRIDGE_GITHUB_ORG` to require membership. The first person admitted is the admin.

**The people and the map.** Inference from git is a fallback; the pilot runs on what the team verified.

- [ ] Every person on the team under **People & ownership** (`POST /api/people`): name, email, GitHub login, Slack member id, the git names they commit under as aliases. A person with no Slack id cannot be reached; a person with no GitHub login cannot sign in.
- [ ] Teams the CODEOWNERS file names (`POST /api/teams`), with their members, so a team listing resolves to people.
- [ ] Authority rows for each area (`POST /api/authority`): who *decides* for a path pattern (`billing/*`), a decision category (`billing`, `pricing`, `security`, `auth`, `privacy`, `compat`, `rollout`, `data`, `infra`, `legal`, `customer`, `ux`, `ops`) or the whole repository; who must *approve*; who *knows*. A verified row outranks git for its scope.
- [ ] A coordinator (`POST /api/settings` with `coordinator`): the person who receives everything nobody is verified to own, with the candidates git suggests, so it can be handed on. Without one, unowned questions sit unrouted.
- [ ] Pilot mode on (`require_verified_route`) until the map covers the areas the pilot touches: every route that rests on git history alone goes to the coordinator instead of pinging someone on a guess.
- [ ] Ingest the repository (`python3 -m bridge ingest /path/to/checkout --repo acme/platform --db "$BRIDGE_DB"`, or **Ingest a repository** in the inbox) so kickoff discovery, blame and review signals exist. Use the `owner/name` identity the agents will use.
- [ ] Sync it from GitHub (`GITHUB_TOKEN=... python3 -m bridge sync acme/platform --db "$BRIDGE_DB"`, and `GITHUB_TOKEN` with `BRIDGE_GITHUB_REPOS=acme/platform` on the server to keep it current every 15 minutes): pull request approvals land on the squash commits that merged, the teams CODEOWNERS names become their members, and descriptions become records. Optionally a repository webhook at `<public-url>/webhooks/github` (`GITHUB_WEBHOOK_SECRET`; events pull requests, pull request reviews, teams, memberships) so a merge is known the minute it lands. `GET /api/sync` shows when each repository last synced; a route built on a sync older than three days says so in its evidence.
- [ ] Read `bridge_list_owners` (or `GET /api/people`) once and check that the people, the authority rows and the coordinator are what you meant.

**Slack, or Teams.** People reply in the Slack thread or in the inbox; the inbox is the record. Teams (`TEAMS_WEBHOOK_URL`, an incoming webhook) is outbound only: every message goes to one channel with a link to the inbox, and people answer in the inbox, not in Teams.

- [ ] A Slack app with `chat:write`, `im:write` and `users:read.email`; its bot token in `SLACK_BOT_TOKEN`, its signing secret in `SLACK_SIGNING_SECRET`.
- [ ] Events API subscribed to `message.im` and `message.channels`, request URL `<public-url>/webhooks/slack`. Bridge answers the URL verification.
- [ ] A fallback channel (`SLACK_FALLBACK_CHANNEL`, or `slack_fallback_channel` under settings): where a question goes when its owner has no Slack id, with a note naming them. Invite the bot to it.
- [ ] Test it: answer a seeded node from the inbox as a person with a Slack id and see the DM; reply `sign off` in the thread and see the node signed.

**The agents.** Each developer's agent connects directly to the shared Bridge with that person's token.

- [ ] Each person mints an **agent credential** under **Connections & setup** (`POST /api/tokens` with a label naming the agent; kind `agent` is the default). It is shown once. The credential kicks off tasks, writes nodes and reads the tree as that person; it cannot answer, sign, hand on, make a rule, mint anything or sign in as them, so a leaked agent token decides nothing. A token for a person's own scripts is `{"kind": "human"}` and carries their role.
- [ ] Add the shared endpoint to each agent's MCP settings. Claude Code accepts `.mcp.json` in the repository (or the user's MCP settings):
  ```json
  {"mcpServers": {"bridge": {"type": "http", "url": "https://bridge.acme.internal/mcp",
      "headers": {"Authorization": "Bearer brg_..."}}}}
  ```
  Cursor and Codex use the same URL and header in their own MCP configuration formats. The setup page shows the exact shape.
- [ ] Read the server instructions the agent sees (`tools/list` and `initialize`, or the setup page): kickoff with `bridge_start_task` and a `client_key`, nodes with `bridge_add_node`, `bridge_settle_node` for what it settles itself, `bridge_wait` once its independent work is done, `bridge_get_tree` before anything irreversible, `bridge_finish_task` at the end.

## While it runs

The inbox's **Needs you** is the queue: open questions, answers waiting on a signature, decisions put in doubt by a correction, in the order they arrived; it never truncates. Beyond it, four things are the operator's to watch:

- **Deliveries** (`GET /api/deliveries?state=failed`): a notification that could not go out, with the reason, and a retry. The usual reason is a person with no Slack id and no fallback channel.
- **Unrouted** nodes: Bridge did not know who owns them and there is no coordinator. Assign them, or set a coordinator.
- **Needs review**: an answer this decision leaned on was corrected. The owner confirms or corrects it; until then the agent is told not to act on it and the task cannot finish.
- **Failed inbound replies** (`GET /api/deliveries`, `inbound_failed`): a Slack reply Bridge could not apply, with its error. Retry it with `POST /api/deliveries/inbound/<event id>/retry`; nothing a person said is lost while it sits there.
- **Referrals** under People & ownership: a hand-on (`not me @person` in Slack, **Hand on & learn** in the inbox) records who decides that scope from now on; it is accepted when the person answers. End a wrong one with `POST /api/authority/:id/end`.
- **Rules**: every decision is request-specific until its owner makes the signed answer a rule (in the decision's inbox view, or `rule if <words> until <date>` in the Slack thread). Only a rule lets a later matching question resolve without a fresh signature; the evidence names it. End a rule that outlived its reason from the same view.
- **Overdue** in the inbox: open questions older than `overdue_hours` (72 by default, `POST /api/settings`). Their owners get a daily reminder in Slack; past twice the window, so does the coordinator.
- **Context for the agent**: a task's view has **Add context for the agent**; the note lands on the tree and a waiting agent returns with it. `GET /api/tasks/:id/trace` is the full record of a task when something needs explaining.
- **Records from elsewhere**: `POST /api/records` for a ticket, a document or a note (with its URL as locator), and `record: <what was decided>` in a Slack channel the bot is in (`slack_capture_repo` names the repository). They are evidence the ladder cites, never sign-off.

## When something goes wrong

| What happened | What the agent sees | What the operator does |
| --- | --- | --- |
| The agent's session restarted | `bridge_start_task` with the same `client_key` returns the same task; `bridge_get_tree` has every node and answer; `bridge_wait` with `since` returns what people did meanwhile | Nothing; the tree is rows in the database |
| The owner answered hours later | The Slack acknowledgement names the task id and `bridge_get_tree`; a waiting agent returns from `bridge_wait` with the change | Nothing |
| An answer was corrected after other tasks reused it | Those nodes come back `needs_review`, with the reason; their tasks cannot finish | The owner of each dependent node confirms or corrects it (a review message reaches them) |
| The owner cannot be reached | The node stays `pending`, naming the owner | Add their Slack id, or set a fallback channel and retry the delivery, or hand the decision on to someone who is reachable |
| Nobody is verified for the area | The node goes to the coordinator, with the candidates git suggests | The coordinator hands it on; the hand-on teaches routing |
| Slack is down | Nothing changes for the agent; people can still answer in the inbox | Deliveries retry with backoff and show as failed after the last attempt; retry them once Slack is back |
| Bridge restarted | Nothing: the agent's next call works against the same rows | Queued deliveries resume when the worker starts; the GitHub sync resumes from its watermark and re-reads a day of overlap |
| A Slack reply could not be applied | Nothing changes; the decision still waits | It is kept as a failed inbound event with its error; Slack's retry or `POST /api/deliveries/inbound/<id>/retry` applies it once |
| Someone replied to an old Slack message | Their reply is refused with what changed, and the current state is sent again | Nothing; they reply in the newer thread or act in the inbox |
| A rule was ended or expired mid-task | The nodes it authorized come back as needs-review, and the task cannot finish | The owner answers or signs them; ending a rule says how many it put back |
| The GitHub sync failed | Routes carry a line saying how old the map is | `GET /api/sync` shows the error; fix the token or the network and `POST /api/sync` (or wait for the next scheduled run, which repeats from the same cursor) |
| A person Bridge does not know replied in Slack | Nothing is recorded | Add their Slack member id to their person row; Bridge told them so in the thread |

## Managed execution

Optional, and the same contract: a task launched from **Start a task** (`--agents`, an OpenAI key) is kicked off on the canvas like any other, every `request_judgment` the hosted agent makes is a node through the ladder, and the hosted agent gets its answer when a person answers or signs the node, not when a record was found. Besides the disposable billing fixture, `--managed-repo NAME=/path/to/checkout` (repeatable) offers a local checkout; the files git tracks at its HEAD are packaged into the hosted workspace, text only, 256 KB per file and 8 MB in all.

## What Bridge enforces, and what it does not

Bridge refuses its own `bridge_finish_task` while a decision waits on a person, and it records what was authorized, by whom, on what standing. It does not stand between the agent and the repository: an agent can still edit files, commit, open a pull request or deploy without asking Bridge anything. For the pilot, keep normal code review as the release gate and read the task's trace (`GET /api/tasks/:id/trace`) when a change looks unsupported. Treat the protocol as guidance the agent follows and the gate as a claim to verify, not as a control that cannot be bypassed.

Day one: connect Slack, ingest the repository and check `bridge_connection_status` from the host agent. Raven imports contacts and infers the first person to ask. No owner map or recipient accounts are required. Configure a Slack triage channel for questions without a reachable contact. Use the optional web overview to inspect routing and delivery failures. See [Slack setup](docs/slack.md).

The gate covers authorization, not conformance. `evals/real_oss` caught a real agent reading an owner's correction on the canvas, reporting that its change matched, and finishing with the gate satisfied while the diff said otherwise. So when you review a change: read the task's trace to see what was decided, and then read the diff against it. A signed node means somebody decided, not that the code follows.

## What the pilot does not get yet

Replies from Teams (its webhook is outbound only); connectors that pull Linear, Jira, Notion or Confluence on their own (records from them arrive through `POST /api/records`); a learned embedding model.
