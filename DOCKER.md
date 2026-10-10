# Docker and PostgreSQL

Start Docker Desktop (or Docker Engine with Compose v2). From this checkout:

```sh
./setup
```

Open the printed URL. First, name your workspace using the setup credential from
`./dev login`. Next, create your personal profile and password; you automatically
become admin. Raven stays locked until both steps finish. Returning users
sign in with email/password or configured GitHub login. Admins invite teammates
from the UI; see [account onboarding and tests](docs/user-onboarding.md).
Setup checks Docker/Compose availability and opens web setup without terminal
questions. Connect GitHub and create agent connection details there. The optional
`--configure` wizard saves Slack and model settings privately in the ignored `.env` file.
Startup builds Raven, starts PostgreSQL 17, waits for readiness, applies the
schema and migrations, and creates a local developer account. It starts with an
empty workspace unless you select a repository or pass `--demo`. Repeated runs
preserve data and skip unchanged repository history. No host Python, Node, Make,
or API key is needed. Noninteractive runs omit the token from logs; `./dev login`
prints it when needed.

## Self-hosting and data flow

Raven is fully self-hosted software. You run its application and database on your
own hardware or in a cloud account you choose. You operate the network, storage,
credentials, backups and access controls. The standard Compose stack stores the
graph in PostgreSQL and private application files in customer-controlled volumes;
a direct Python deployment can use SQLite. No Raven-operated application backend,
database, hosted token broker, telemetry collector or remote license check is
required by the shipped service.

A Raven license grants software-use permissions. It does not supply infrastructure,
inference credits or integration accounts. Hosting, inference and third-party
service arrangements and charges are yours. There is no requirement to host Raven
with its authors.

### Services you enable can receive data

The deployment and the providers you connect define where information is processed.
Their own access, retention and data-processing terms apply; self-hosting Raven
does not change them. The shipped paths are:

- **Inference:** Raven's model client sends selected task, source, conversation and
  diff context directly to the Anthropic Messages API using your key, or through
  your installed Claude CLI/account. These are the currently supported internal
  model backends, not a promise of arbitrary model-endpoint compatibility. The
  Compose default is `BRIDGE_MODEL_API=none`. Direct Python configuration defaults
  to Anthropic and can fall back to an installed Claude CLI; set
  `BRIDGE_MODEL_API=none` explicitly when you want Raven's internal model calls off.
  Local search embeddings are computed inside Raven without an embedding API.
- **Coding agents and optional managed execution:** a connected Claude Code,
  Codex, Cursor or other MCP host has its own provider and data settings. The
  opt-in managed Agents API sends the selected task, packaged repository files
  and tool results to OpenAI and runs the agent in an OpenAI-hosted environment.
  That optional execution environment is outside your Raven server.
- **Slack and Teams:** enabled messaging sends questions, context, review material
  and replies through the configured workspace/service. Slack directory and search
  calls also reach Slack. Teams bot mode contacts Microsoft's authorization/key
  services as well as its delivery service; a Teams webhook uses its configured
  destination. These are customer-configured integrations, not Raven-hosted relays.
- **GitHub:** sign-in, device authorization and repository sync contact GitHub
  directly; configured sync can poll in the background. The default connection
  uses the shared registered Bridge Repository Access App. Local token storage does not make that App registration yours;
  review its GitHub permissions, or [use your own registered App](docs/github-connection.md#optional-use-your-own-app)
  or the documented operator credentials. This default registration is separate
  from hosting Raven's application or database.
- **Jira and Airweave:** Raven polls Jira only for projects an administrator adds as a
  [native context source](docs/context-sources.md), with the `JIRA_*` API token you
  set. Records imported by a coding host use that host's connector and provider permissions. Optional
  Airweave retrieval sends searches to the `BRIDGE_AIRWEAVE_URL` you configure,
  which may be your own Airweave service or a chosen hosted service. Airweave's
  upstream connectors and processing are part of that separate deployment.
  Enabled retained sources can also be refreshed in the background.
- **Browser voice:** optional microphone recognition and speech synthesis can use
  the browser/OS provider. Raven stores the resulting text, not audio. See
  [voice privacy and browser behavior](docs/voice-interviews.md#privacy-and-browser-behavior).
  Typed input avoids the browser speech path.

The web application's assets are served by your Raven instance, rather than a
required third-party asset CDN. Browser integrations, extensions, coding-host
telemetry and provider-side processing have their own behavior; this is not a
zero-egress or air-gapped certification. Choose integrations and network policy
that fit the information you intend to send.

### Installation and updates

Cloning or fetching updates contacts the configured Git remote. Docker builds and
rebuilds may download base images, Debian packages and Python dependencies from
the configured registries and package sources. This installation traffic is
separate from runtime task-data calls. The shipped service has no Raven-operated
automatic updater or usage-reporting endpoint. Disabling inference alone does not
disable configured integrations or package downloads.

## One setup command

```sh
./setup                                      # Configure sources and agents; start empty
./setup --demo                               # Add sample tasks, decisions, and Git evidence
./setup --yes                                # No prompts, offline defaults
./setup --configure --project /path/to/your/code                          # Revisit optional connections
./setup --repo /path/to/platform --repo-name acme/platform
./setup --port 7444                          # Change port and local public URL together
./setup --import-sqlite /path/to/bridge.db    # Explicit migration into an empty destination
```

The same entry point is available as `./dev setup`; `./dev config` reopens the
configuration wizard. Blank answers keep existing source and integration settings.
Choosing Anthropic asks for a hidden API key and an optional, non-secret Anthropic
workspace ID for multi-workspace keys. The ID is saved as `ANTHROPIC_WORKSPACE_ID`;
Enter keeps a saved ID or leaves it unset. The same prompt is available through
`.\setup.ps1 --configure` on Windows/WSL. This is Anthropic's workspace ID, not
the Raven workspace name supplied with `--workspace`.
Select one or more agent clients to write URL-based Raven entries into their
project MCP configuration; each keeps unrelated MCP servers. The endpoint is
`BRIDGE_PUBLIC_URL/mcp` and requires a limited agent token. Setup writes that
credential into each selected project's local MCP configuration with owner-only
permissions, and excludes the file from the local Git checkout so it is not
accidentally committed. It refuses to alter an already-tracked MCP config for
this reason. Treat these files as secrets; on another machine, rerun setup or
mint a separate agent token in the web UI. On the default loopback URL, clients
must run on the same machine; another machine needs a reachable HTTPS deployment.
API credentials are entered without echo and saved in `.env` with owner-only permissions. Shell
environment variables take precedence over `.env`, as in normal Docker Compose.
To disconnect an integration, remove its credentials from `.env` and rerun setup.

Docker must already be installed and running; setup gives an actionable error if
it is missing. First build needs internet access. It does not install system
software or register third-party applications. The built-in GitHub connection
needs no callback URL; Slack requires a reachable HTTPS Events API URL. See [Slack setup](docs/slack.md). Use `./setup --workspace "Your team" --configure` for headless workspace creation; recipients do not need Raven accounts.
The wizard saves credentials; readiness verifies the app/database, not external
credential validity. To connect GitHub, sign in to Raven, open Connections & setup,
and select Connect GitHub. The registered Bridge Repository Access app is included
by default; see [GitHub connection setup](docs/github-connection.md).
Users select repositories, then authorize a one-time device code on GitHub.
Raven stores user credentials in `/data/github-user.json` with owner-only
permissions and automatically refreshes expiring tokens. Protect that file in
backups. The App polls for changes and needs no public webhook or hosted broker.
Existing operator-owned App keys and the `GITHUB_TOKEN` plus
`BRIDGE_GITHUB_REPOS` server settings remain supported. Jira records can be imported through `POST /api/records`;
there is no built-in Jira polling connector, so setup does not offer one.
Repository paths entered in the wizard must be absolute macOS/Linux/WSL paths;
for Windows paths use the PowerShell wrapper's `--repo` flag. Use a standalone
Git clone: linked worktrees with Git metadata outside the mounted directory need
additional mount configuration.
Managed execution is opt-in, installs its SDK, and enables the bundled disposable
billing fixture; it does not automatically authorize agents to edit your checkout.
Configure external agent clients through the app's **Connections & setup** page.

## Portability and access

Use `./setup` on macOS/Linux or from WSL. Windows PowerShell users can run
`.\setup.ps1` with the same flags; this forwards to WSL and translates repository
and import paths. Docker Desktop needs integration enabled for that distribution.
The Windows wrapper has not been tested on a Windows host.

The application is reachable at the printed localhost URL, which is the address
Raven was configured with rather than the port mapping: Docker publishes
`BRIDGE_PORT` while the container always binds 7333, so `BRIDGE_PUBLIC_URL` is
the only thing that tells the server which port people reach it on. `./setup`
keeps the two together and `./dev up` warns if they have drifted apart. Both
spellings of the loopback address work — `http://localhost:PORT` and
`http://127.0.0.1:PORT` are the same machine — and any other host is refused.

For a Docker host on another machine, SSH forwarding preserves the local
binding:

```sh
ssh -L 7333:127.0.0.1:7333 user@your-server
```

Then open `http://localhost:7333` on your laptop. A publicly shared deployment
needs its own domain, HTTPS proxy, public URL, and production sign-in configuration.
Clone the repository to reproduce the runtime on another machine; use backup and
restore below to move existing data. Git does not carry database volumes or `.env`.

With `--demo`, the seed contains five tasks, six decisions (including signed memory and pending
sign-offs), three sample owners, seven authority assignments, repository evidence,
and a local developer administrator. Integration tables exist but remain empty until connected. Seeding
does not call models or execute coding agents. The administrator can manage the
directory; deciding for someone else requires an explicit, recorded admin override.

## Data organization

The database contains 39 tables split by concern:

- Tasks/memory: `runs`, `decisions`, `decision_revisions`, `decision_links`, `node_claims`, `events`.
- Identity/authority: `people`, `owners`, `teams`, `team_members`, `authority`, `api_tokens`, `account_invites`, `account_passwords`, `settings`.
- Repository evidence: `engineers`, `artifacts`, `intents`, `intent_paths`, `ownership`, `changes`, `change_paths`, `change_people`, `listings`, `blame_lines`, `connector_sources`, `fetched`, `model_cache`.
- GitHub: `gh_pulls`, `gh_users`, `sync_state`.
- Messaging: `notifications`, `webhook_receipts`, `reply_readings`.
- Managed execution: `executions`, `provider_calls`, `provider_events`, `deliveries`.
- Migrations: `schema_migrations`.

Decisions and records have generated `tsvector` columns and GIN indexes replacing
SQLite FTS5. Local hashed embeddings are stored as `BYTEA` and scored in Python.
There is no SQLite database in the Docker runtime; file-based SQLite commands
remain supported for existing local setups.

## Everyday commands

```sh
./dev status           # Health and published port
./dev logs             # Follow application logs
./dev seed             # Idempotent bootstrap; preserves existing tasks
./dev down             # Stop containers; retain data volumes
./dev up               # Rebuild/restart with existing data
./dev db               # PostgreSQL shell; \dt lists tables
./dev shell            # Application container shell
./dev test             # Existing offline suite with isolated SQLite databases
./dev test-postgres    # Behavioral contracts on isolated PostgreSQL schemas
```

Make targets are also available (`make up`, `make login`, `make test-postgres`,
etc.). The Docker-only `./dev` wrapper avoids host Make/Xcode requirements.

Data persists in `bridge_postgres_data`. The generated development login token
persists in `bridge_app_data`, readable only by the container user; PostgreSQL
stores its hash. Down/up and repeated seeding preserve both. Docker's
`docker compose down -v` **deletes these volumes**; none of our commands uses it.

## Configuration and repository ingestion

Optionally copy `.env.example` to `.env` and edit it. The file is ignored by Git
and excluded from Docker builds. If changing `BRIDGE_PORT`, also change
`BRIDGE_PUBLIC_URL` to match. The app is published on host loopback only; PostgreSQL
has no host port. The default database password is for local development. Set a
URL-safe password before initializing a new volume; changing `POSTGRES_PASSWORD`
later does not rotate an existing PostgreSQL role.

To index your own checkout automatically, set `BRIDGE_REPOS_DIR` to the directory
containing it, `BRIDGE_INGEST_REPO_DIR` to its directory name, and optionally
`BRIDGE_INGEST_REPO` to its `owner/repository` identity in `.env`. Then run:

```sh
./dev up
```

Checkouts are mounted read-only at `/repos`. Startup refreshes the graph only
when HEAD changes. Startup, repeat ingestion, and later local history reads use
the same [command-scoped Git safe-directory exception](https://git-scm.com/docs/git-config#Documentation/git-config.txt-safedirectory)
for the exact selected checkout, so host/container ownership differences work
without changing global Git configuration or repository permissions. Git helpers
(hooks, filesystem monitors, external diff/text conversion, and signature
verification) are disabled for those reads. Ingestion never fetches missing
objects: materialize the needed history and listing-file objects in your checkout
before retrying. Git without `--no-lazy-fetch` support (before Git 2.45) accepts
full checkouts without partial-clone/promisor configuration; configured partial
clones require a supporting Git version. The installed capability is checked
before reading objects. A failed history read leaves the previous graph intact.
Unpinned ownership listings must resolve inside the selected checkout; an
external symlink is rejected.
The bundled example remains available when `BRIDGE_DEMO=1`.

GitHub and Slack credentials can be supplied through `.env`; Compose passes the
integration variables to the app. Set `BRIDGE_MODEL_API=anthropic` and
`ANTHROPIC_API_KEY` to enable model-assisted retrieval. Defaults are offline.

For a team deployment, disable `BRIDGE_DEV_LOGIN` and `BRIDGE_DEMO`, configure
GitHub sign-in or a bootstrap admin token, and supply the public URL and TLS proxy.
This is a local development Compose stack, not a multi-tenant deployment.

For optional managed execution, use `./setup --configure --project /path/to/your/code`, or set
`BRIDGE_INSTALL_AGENTS=true`, `BRIDGE_AGENTS=1`, and `OPENAI_API_KEY` in `.env`
before running setup. The default image does not install the optional SDK.

## Import an existing SQLite workspace

Before the first `./dev up`, with an empty PostgreSQL destination:

```sh
./dev import-sqlite /absolute/path/to/.bridge/bridge.db
./dev up
```

The importer starts PostgreSQL and mounts the source directory read-only (including
WAL files). It takes a consistent snapshot, copies application tables in foreign-key
order, retains IDs/history/credentials, resets sequences, and rebuilds native search.
It refuses a populated destination and leaves the source untouched. Stop source
writers for cutover so later SQLite writes aren't left behind. Failed copies roll
back; legacy imported rows receive normal Raven backfills afterward. Existing
SQLite workspaces are never imported implicitly.

## Backup and restore

```sh
./dev backup
./dev restore backups/bridge-YYYYMMDD-HHMMSS.dump bridge_restored
docker compose exec db psql -U bridge -d bridge_restored
```

Backups use `pg_dump` custom format in the ignored `backups/` directory. Restore
creates a new database and fails if its name already exists. To serve the restored
copy, override the app's `DATABASE_URL` to point to `bridge_restored` and recreate
the app. The SQLite `bridge backup` command refuses PostgreSQL and directs you to
`pg_dump`. JSON history export works on either backend.

## Backend implementation

Outside Docker, install `requirements-postgres.txt`, set `DATABASE_URL` to a
PostgreSQL URL, and run the normal commands. Explicit `--db` overrides the environment.
Startup logs redact database credentials, and the browser never receives the URL
in generated MCP configuration. Agents can connect through HTTP/MCP with agent tokens.

The SQL adapter uses native upserts and constraints. PostgreSQL advisory transaction
locks preserve existing read/check/write guarantees, and managed workers use a
session advisory lock instead of a file lock. Routing reads bypass SQLite's
data-version cache to see other connections' commits. These choices preserve
correctness; moving to PostgreSQL alone does not establish horizontal scalability.
