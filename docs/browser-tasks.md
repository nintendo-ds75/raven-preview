# Work directly in Raven

Open **New task**, choose a repository, and describe what you want done. Raven
runs its own agent using the Anthropic Messages API. You do not need Claude Code,
Codex, a terminal session, or an MCP client open.

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

People still answer through Slack or the inbox. Remembered answers and proposals
still need the same authorization as tasks started through MCP. The worker stops
spending inference while waiting, then resumes when decisions, notes or reviews
change. The web page can be closed. Pause stops after the current model call or
isolated command; an interrupted server leaves a saved task that can be resumed.

## Run locally

Configure `ANTHROPIC_API_KEY` and `BRIDGE_MODEL_API=anthropic` in the server
environment. `BRIDGE_MODEL` selects the agent model; the live browser trial used
`claude-sonnet-4-6`. `ANTHROPIC_WORKSPACE_ID` selects a workspace when required. Never paste
keys into the task prompt. Use the existing Slack configuration for notifications.

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

Set `BRIDGE_WORKBENCH=1`, `BRIDGE_MODEL_API=anthropic`, `ANTHROPIC_API_KEY`,
`BRIDGE_REPOS_DIR` to the checkout, and
`BRIDGE_INGEST_REPO=your-org/your-project` in the existing deployment environment.
The regular Compose stack supports repository reading and drafting.

For isolated test commands, use the opt-in overlay:

```sh
docker build -t raven-workbench-base .
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
one model turn at a time and pauses a task after 60 calls until someone resumes
it. This first version is intended for a supervised team, not a high-throughput
multi-tenant execution service.

The built-in agent uses [Anthropic's documented client tool protocol](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
Its file operations and isolated commands are Raven tools; decisions run through
the existing MCP implementation. Model review remains advisory, and the saved
test claims remain distinct from approval.

The provider request enables [automatic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) for the growing conversation. Token and cache usage returned by the provider are saved with each inference step. This is separate from Raven’s decision memory and grants no authority.
