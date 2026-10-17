# Getting started

Install Raven, connect one coding agent, and run one task. About 15 minutes.

```sh
git clone https://github.com/nintendo-ds75/raven-preview.git
cd raven-preview
./setup
```

## Requirements

- Docker Desktop (macOS, Windows with WSL 2) or Docker Engine 24+ with Compose v2 (Linux).
- 4 GB of free memory and about 3 GB of disk for the images.
- Internet access for the first build.
- Python 3 on the machine that runs your coding agent (for the task hooks; not needed by the server).

No host Node, Python package, API key or ownership spreadsheet is required to start.

## 1. Install

`./setup` builds the application image, starts PostgreSQL 17, waits for both to be
healthy, applies the schema and opens the browser. The first build downloads the
base images and a small local embedding model; allow 5–10 minutes. Later starts take
seconds.

When it finishes you will see:

```text
Raven is running.
  Open:              http://localhost:7333
  First sign-in:     ./dev login   (prints the one-time setup credential the browser asks for)
  Connect an agent:  ./setup --configure --project /path/to/your/code
  Later:             ./dev status · ./dev logs · ./dev down · ./dev up
  Guide:             docs/getting-started.md
```

In an interactive terminal the setup credential (`brg_…`) is printed just above
this banner; `./dev login` prints it again, with the page it belongs on.

Windows: run `.\setup.ps1` from PowerShell with Docker Desktop's WSL integration on.
Port taken: `./setup --port 7444`.

## 2. Create your workspace

The browser opens **Create your workspace**.

1. Enter a workspace name and the setup credential from `./dev login`. The credential
   proves you operate this installation; nobody else can claim the workspace. It is
   not a sign-in: the email and password form on **Sign in** is for step 2's account.
2. Create your profile: name, email and a password of 12 characters or more. The
   first profile is the admin.

Raven stays locked until both steps are done. Returning users sign in with email and
password; admins invite teammates from **Connections & setup**. Details and the
headless alternative (`./setup --workspace "Your team"`) are in
[workspace accounts](user-onboarding.md).

## 3. Connect your coding agent

From the checkout your agent works in:

```sh
./setup --configure --project /path/to/your/code
```

Choose `claude`, `codex` or `cursor` when asked; the wizard then offers optional keys
(Anthropic, Slack, Teams, OpenAI), and Enter skips each. Setup writes the MCP
configuration for that client into the project (Git-excluded, with a limited agent
credential) and, for Claude Code and Codex, installs the task hooks that register each
task with Raven before the model starts. The hooks name the repository after the
checkout's `origin` remote; a checkout without one needs
`BRIDGE_INGEST_REPO='owner/name'` in Raven's `.env` first. Restart the agent and
trust the project when it asks.

Or do it from the browser: **Connections & setup → step 1 → Connect** shows the one
command for your client:

<details><summary>Claude Code</summary>

```sh
claude mcp add --transport http bridge http://localhost:7333/mcp --header 'Authorization: Bearer brg_…'
```
</details>

<details><summary>Codex</summary>

```toml
# ~/.codex/config.toml or .codex/config.toml
[mcp_servers.bridge]
url = "http://localhost:7333/mcp"
http_headers = { Authorization = "Bearer brg_…" }
```
</details>

<details><summary>Cursor</summary>

Click **Add to Cursor**; Cursor asks you to confirm the installation.
</details>

Then add the Raven rule to the repository's `CLAUDE.md` or `AGENTS.md` (the browser
offers it as **Copy instruction**). Without it an agent can hold Raven's tools and
never call them:

```markdown
## Raven

This team uses Raven (the `bridge` MCP server) to route the judgment calls in a task
to the person who owns them. At the start of every task, before reading or editing
anything, call `bridge_start_task`, then follow what Raven returns for the rest of
the task.
```

Credentials: the browser mints a limited **agent** credential. It can register
tasks and write decisions as you; it cannot answer, sign or invite anyone.

## 4. Give Raven context

Raven routes decisions using evidence, not a hand-built map. Give it at least one:

| Source | What it adds | How |
| --- | --- | --- |
| GitHub | Pull requests, reviews, CODEOWNERS; keeps syncing | **Connections & setup → Connect GitHub**, choose repositories |
| Local clone | Commit history, blame, CODEOWNERS | `./setup --repo /path/to/checkout --repo-name owner/name` |
| Slack channels, Jira projects, docs sites | Evidence the agent can cite | `python -m bridge.sources scan --repo owner/name` suggests them; an admin connects each one. See [context sources](context-sources.md) |

## 5. Run one task

In your coding agent, ask for something that needs a decision from someone other
than you, for example:

> Add usage-based pricing to the billing module.

What happens:

1. The hook registers the task. Raven returns **engage** or **pass** with its reason.
2. As the agent finds decisions a person must make, each appears in **Inbox** with
   the agent's context, what Raven found in the records, and the person its evidence
   names. If Slack is connected, that person gets a short notification with a link.
3. The person answers, signs or hands the decision on. The agent is woken with the
   answer and continues; `bridge_finish_task` is refused while a decision is unsigned.
4. **Tasks** shows the whole run: questions, answers, who decided, and the review of
   the submitted diff.

## Operate

```sh
./dev status      # containers and health
./dev logs        # follow the application log
./dev down        # stop; data is kept
./dev up          # rebuild and start with the saved settings
./dev login       # print the setup credential again
./dev backup      # pg_dump into backups/
```

Upgrade: `git pull && ./dev up`. Migrations are additive and run on start.

## Troubleshooting

**`./setup` stops before building.** "Install Docker Desktop…" means Docker is not
installed; "Docker Compose v2 is required" means the plugin is missing (on Linux,
install `docker-compose-plugin`); "Start Docker Desktop or the Docker daemon" means
it is installed but not running.

**The browser shows 403 or a blank page.** The address you opened is not the one
Raven was configured with. Open the URL `./setup` printed, or run
`./setup --port <port>` so the published port and `BRIDGE_PUBLIC_URL` match.

**The agent has the tools but never calls them.** Add the Raven rule to
`CLAUDE.md` or `AGENTS.md` (step 3), and for Claude Code or Codex restart the agent so
it loads the hooks. `./dev logs` shows each hook call as a `POST /mcp`.

**A decision is "unrouted".** Raven found no evidence naming an owner. Connect
GitHub or ingest the repository, or set a coordinator in **People & ownership** who
receives everything nobody is known to own.

**Nothing arrives in Slack.** Slack is optional and needs a public HTTPS URL for
Raven; see [Slack setup](slack.md). People can always answer in the inbox.

Still stuck: `./dev status` and `./dev logs`, then open an issue with the output.

## Next

- [Slack notifications](slack.md) · [GitHub connection](github-connection.md) · [context sources](context-sources.md)
- [Work in the browser](browser-tasks.md): run Claude inside Raven without a separate agent
- [Host adapters](host-adapters.md): how the hooks register tasks and resume idle sessions
- [Running a pilot with one team](../PILOT.md) · [Docker, hosting and backups](../DOCKER.md)
