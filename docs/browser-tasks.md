# Work directly in Raven

Open **New task**, choose a repository, and describe what you want done. Raven
runs the selected coding harness on the Raven server: Claude Agent SDK, Pi SDK, or
OpenCode. Managed Codex execution is currently disabled because its pinned native
tool-suppression controls do not establish the required parent-only boundary. See [execution harness coverage](execution-harnesses.md) for exact support and verification. You do not need a separate agent window or terminal open.
**Ask a question** reads connected evidence without starting a coding harness.

The conversation shows progress and accepts follow-up messages. The overview
shows recorded decisions, answers, approval status and people involved. Open
**Full history** for source evidence, contact history and the advisory diff
review. **Download patch** returns the change for your normal code-review flow.
Before showing the result, Raven gives its agent the actual finish report and
rechecks the current patch and approvals. An incomplete advisory review pauses
the task. Continuing can retry it or prepare the patch for manual review, with
the incomplete review clearly marked in the result. It is never presented as a
successful check, and this does not bypass owner approval or discovery gates.

Raven works on a separate snapshot of committed `HEAD`. It does not edit the
original checkout, push a branch, merge a change or deploy. Uncommitted files, working-tree edits and Git history are not copied into the task.
The agent can read committed files, so use the same repository access boundary
you would for an external coding agent. Server credentials are never passed to
the command container. The current
snapshot limit is 16 MiB, 4,000 files and 1 MiB per file; symlinks are refused.

People answer through the Raven inbox; Slack is optional. Remembered answers and proposals
still need the same authorization as tasks started through MCP. The worker stops
spending inference while waiting, then resumes when decisions, notes or reviews
change. The web page can be closed. Pause requests host interruption; an already
running isolated command may finish. A server restart leaves saved work paused.
Uncertain tool outcomes block automatic replay until reconciled. A disconnected
host is never reported as completed just because its process stopped.
If a host ends its turn with unanswered decisions without calling the wait tool,
Raven still saves a waiting state and watches for a decision update. Waiting does
not require repeated inference calls. Uncertain operations appear on a paused
task with the exact tool and arguments. An admin must inspect the actual effects,
record an observed outcome and evidence, then explicitly continue. That inspection
is an attributed operator statement, not a replacement test receipt or approval.

## Run locally

Install `requirements-hosts.txt` for Claude Agent SDK 0.2.165. Configure
`RAVEN_HOST_CLAUDE_API_KEY` and `RAVEN_HOST_CLAUDE_MODEL` on the server. The live
SDK smoke test used `claude-sonnet-4-6`. Set `RAVEN_HOST_CLAUDE_WORKSPACE_ID` if
required by the test account. Never paste keys into a task prompt.

**Managed Codex cannot be enabled by credentials or installation.** Its runtime
is retained for isolated acceptance research only. The test suite installs the
optional exact package with `npm ci --prefix hosts/codex --ignore-scripts` and
supplies its own dummy credential and loopback inference fixture. No deployment
key is needed or requested for those tests.

The research transport uses the experimental dynamic-tool interface and rejects
changed effective model/provider identities. Native built-ins can remain enabled
despite suppression flags, so read-only sandboxing is not a sufficient read or
network boundary. Production dispatch is unconditionally blocked.

Host credentials and model choice are separate from Raven's internal evidence
reading. Internal inference uses `BRIDGE_MODEL_API=anthropic` with
`ANTHROPIC_API_KEY`, or `BRIDGE_MODEL_API=openai` with `OPENAI_API_KEY`. Set
`BRIDGE_MODEL` and `BRIDGE_FAST_MODEL` to models available on that provider.
Missing credentials no longer silently select a local Claude login. Explicit
`BRIDGE_MODEL_API=claude-cli` remains a local operator choice.

The browser reports setup blockers for each host without making a provider call.
A ready configuration is not proof of provider authentication. Selecting an
unavailable host gives its actual blocker and never runs another host instead.

```sh
python -m bridge serve --workbench \
  --managed-repo your-org/your-project=/absolute/path/to/checkout
```

The operator chooses repositories once. Task authors choose their name in the
browser; they do not need to name paths or owners. Context ingestion and GitHub,
Jira, Slack and document connections remain the normal Raven context layer.

To run tests as well, install Docker and prepare an image containing the project's
test dependencies and a `python` executable. Commands have no network access, so
install dependencies when building that image, not during the agent's task.

```sh
docker pull python:3.13-slim-bookworm
BRIDGE_WORKBENCH_SANDBOX=docker \
BRIDGE_WORKBENCH_IMAGE=python:3.13-slim-bookworm \
python -m bridge serve --workbench \
  --managed-repo your-org/your-project=/absolute/path/to/checkout
```

The basic Python image supports standard-library tests. Projects needing pytest,
Node or other tooling should use a prepared project image instead. Without a
sandbox, Raven can investigate and draft files, but its transcript says tests
could not be run. It must not label those changes tested.

## Run in Docker

Set `BRIDGE_WORKBENCH=1`, the host variables above,
`BRIDGE_REPOS_DIR` to the directory containing your checkouts,
`BRIDGE_INGEST_REPO_DIR` to the checkout's directory name, and
`BRIDGE_INGEST_REPO=your-org/your-project` in the existing deployment environment.
For example, a checkout at `/srv/repos/project` uses
`BRIDGE_REPOS_DIR=/srv/repos` and `BRIDGE_INGEST_REPO_DIR=project`.
Ingestion and the browser task runner use that same checkout.
The regular Compose stack supports repository reading and drafting.

For isolated test commands, use the opt-in overlay:

```sh
docker build --build-arg INSTALL_HOSTS=true -t raven-workbench-base .
docker pull python:3.13-slim-bookworm
docker compose -f compose.yaml -f compose.workbench.yaml up --build -d
```

This overlay gives the **trusted Raven server** access to the Docker daemon and
runs that service as root. Use it only on a host dedicated to Raven; daemon
access is administrative access to that host. Generated commands run separately
as an unprivileged user, with no network, no host mounts, a read-only root,
bounded CPU/memory/processes and a two-minute limit. Only task files cross into
and out of that container. Keys, the database and the Docker socket do not.

Task conversations and tool receipts are stored in the existing database;
private copies live under `/data/workbench` in Compose or `.bridge/workbench`
locally. Back up both when you need to resume browser tasks after moving hosts.
`BRIDGE_WORKBENCH_DIR` can select another persistent directory. The server runs
one host turn at a time. Claude has a per-task dollar budget (default $5), and
enabled adapters have a tool-call limit and a 15-minute turn timeout. Pi and
OpenCode provider-reported costs are not a hard spending guarantee; configure
provider-side limits. The disabled Codex research transport has no priced-dollar
enforcement. This version is intended for a supervised team, not a high-throughput
multi-tenant execution service.

Claude uses the [official Agent SDK](https://code.claude.com/docs/en/agent-sdk/python)
with built-in tools disabled and only Raven's in-process MCP tools enabled.
Codex uses the [app-server protocol](https://learn.chatgpt.com/docs/app-server)
over private stdio. Harnesses receive a dedicated home, one provider credential,
no database or connector credentials, and an empty working directory. Enabled Claude/Pi/OpenCode adapters mediate task file and command access through
Raven tools. This claim does not apply to the disabled Codex research transport. Provider reasoning is not
exported. Session IDs, tool IDs, versions and reported usage are saved in Raven.

Local harness sessions send selected source text, prompts and tool results to
the configured inference destination. Cloud providers mean remote inference;
Pi can instead use an explicitly configured self-hosted endpoint. Neither choice
alone establishes an air-gapped deployment—review connectors and runtime setup.
