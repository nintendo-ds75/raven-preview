# Reliable task registration and optional background resume

MCP gives the coding agent Raven's tools. A host hook registers the task before
the model starts working, so kickoff no longer depends on the model remembering
an instruction. The agent still has to inspect the repository and discover
which decisions need people.

## Separate observations from decisions

Create a node for a judgment or authorization choice, such as a policy,
default, compatibility tradeoff, choice of authoritative source, or whether
uncertain evidence is acceptable. Code and test observations that can be
established by inspection belong in the relevant decision's `context`, with
the repository revision, path/lines or exact record/version, and any limits.
Use `facts` only for known current applicability facts. Neither a citation nor
an applicability fact grants authority.

Keep the factual findings separate from the requested judgment in that context;
do not make each finding an approval node. `parent_id` and `depends_on` describe
actual decision relationships and authorization prerequisites, not a checklist
of evidence to read. The `data-source` category has the same approval rules as
other categories. An agent-settled node remains unsigned and can block finish.

If no human judgment is needed, keep the registered task, do the normal work
and checks, read its tree, and finish through the existing protocol without
inventing a node. Existing open decisions and required follow-ups still apply.
This guidance does not remove a mistakenly created node or authorize its answer.

The same generic guidance is delivered by MCP initialization and the installed
registration hook. Rerun setup to refresh an older copied host adapter; restart
or reconnect the client so it reads current server instructions.

## Connect a local host

`./setup --configure --project /absolute/path/to/code` installs the MCP
configuration and hooks for the selected Claude Code or Codex client. Cursor
gets MCP configuration only. The checkout must have a repository remote, or the
operator must set `BRIDGE_INGEST_REPO=owner/name` in Raven's private `.env`.

Hooks require Python 3 on the **coding host**. Raven's Docker server does not
otherwise require a host Python installation. To use instruction-based MCP
without hooks, set `BRIDGE_HOST_HOOKS=0` in `.env` and reconnect. Setup refuses to
write a credential into a tracked config file. Adapter files are private and
added to this checkout's Git exclude list; keep them out of shared archives too.

Restart the host and trust the project using its normal trust flow. Raven never
bypasses hook approval or host tool permissions. Host versions and settings must
support the documented [Claude hooks](https://code.claude.com/docs/en/hooks) or
[Codex hooks](https://learn.chatgpt.com/docs/hooks). An installed file is not proof
that the host loaded it. Verify that one ordinary prompt appears as one task in
Raven, and that a follow-up stays on that same task.

The adapter verifies MCP initialization and tool discovery, binds the
credential's authenticated identity to the host session and checkout, and
registers the exact first prompt with an idempotent client key. Reconnects and
compaction recover that task. No paths, owner names or decision list are required
from the user. Unavailable Raven blocks prompt submission visibly in hosts that
honor the hook's failure response. Codex also marks the MCP server required.

The command adapter shares a 45-second budget across checkout verification,
MCP initialization, tool discovery and task registration, below the installed
60-second host timeout. It returns an explicit blocking failure when that
budget expires, including when a response body stalls. Socket timeouts alone
would let several slow calls exceed the host's ceiling. Retry the same prompt
after checking the connection; if the server completed an interrupted request,
the same session/event identity recovers its task rather than creating another.
The budget starts when the host has delivered the callback input. A frozen or
terminated host process remains outside the adapter's guarantees.

The first-prompt gate is `UserPromptSubmit`. `SessionStart` is a connection and
recovery check; Claude does not block a session on its exit-2 error. Claude also
lets a command-hook timeout proceed without the hook's context, which is why
the adapter must report its own error before that timeout. Hook files must be
loaded and trusted by the actual host. Codex project hooks additionally require
trust of their current definition; cloud-orchestrated Work/dot does not run
project-local command hooks. An installed configuration alone is not a verified
connection.

For a separate task in the same conversation, start a prompt with:

```text
Raven new task: add a bounded retry budget
```

A normal follow-up never creates a second task. Moving a session to another
checkout is refused; reconnect with a fresh host session there.

The adapter reports configured MCP server names and verifies Raven's own tools.
Other server names are only an inventory, not proof of their permissions or
availability. It does not crawl unconfigured services, request OAuth access, or
read other connectors' credentials.

## Keep the agent waiting while a person answers

The adapter also installs `Stop` and `SessionEnd` hooks. While a decision on the
session's open task waits on a person, `Stop` is refused and the agent is told
to call `bridge_wait` with the task id, so an answer given in Slack reaches an
agent that is still running. The refusal names only the task id and a count;
question and answer text stays on the tree. The hold lasts at most
`BRIDGE_STOP_HOLD_MINUTES` in total (default 20, set where Raven runs). A new
prompt from the person at the keyboard starts it afresh. Past the budget the
agent stops, and a later answer resumes it as described below.

`Stop` and `SessionEnd` fail open. If Raven is unreachable or slow, the hook
returns no decision and the host stops as it would without Raven. Only
`UserPromptSubmit` registration blocks visibly on failure.

## Resume an idle coding session after a person replies

Raven keeps a durable mailbox for the authenticated host session. Human answers,
signatures, referrals, required follow-ups and source changes produce resumable
work. A late correction can wake a completed task too. Only one Raven supervisor
can lease a message at a time; acknowledgement advances its cursor. An answer
the agent already read on the tree while it was held does not resume it again.

Stop the interactive host first, then deliberately enable a local supervisor:

```sh
cd /absolute/path/to/code
python3 .raven/host.py watch --host claude --all --allow-background
# Or one exact session: --session EXACT_SESSION_ID. Use --host codex for Codex.
```

With `--all`, the supervisor resumes every session of this credential in this
checkout that has **stopped** (its `Stop` or `SessionEnd` hook reported so). A
session that is working, or held waiting on a person, is left alone.

The supervisor polls every 15 seconds and runs the installed host's own resume
command in the bound checkout, in the host's automatic mode:

| Host | Command |
| --- | --- |
| Claude Code | `claude --print --resume <session> --permission-mode auto --output-format json "<prompt>"` |
| Codex | `codex exec --sandbox workspace-write -o <file> resume <session> "<prompt>"` |

Claude Code's auto mode is not blanket approval. A classifier approves
ordinary work in the checkout and blocks risky actions such as force pushes,
deploys and writes to protected paths. A headless run cannot ask anyone, so a
blocked action is denied and the run continues without it. Auto mode needs a
supported model; on an older model the session starts in manual mode and
edits are denied. Codex runs inside its workspace-write sandbox. Source text
cannot specify a shell command. Ctrl-C stops background execution. Raven's
server never starts a process on the customer's machine by itself.

### The report back to Slack

After each resume, the supervisor reports what happened. Raven records it on
the task (`host_resume_report`) and posts it once to every Slack thread it
started for the task:

- who answered and what they said;
- the agent's final message, quoted, capped and stripped of mentions (it is
  the agent's claim, not Raven's);
- the files the run changed against where it started, with line counts;
- any actions the host's permission mode denied, with the command to resume
  the session yourself and approve them;
- how many decisions still wait on people, and a link to the task.

A failed resume is reported too, and its message is released for a later
attempt. Claude Code reports denied actions in its JSON output; Codex reports
only its exit status and final message.

Delivery is **at least once**. A failed process releases the message; an abandoned
lease can be claimed after 30 minutes. One invocation is bounded to 20 minutes.
A crash after host success but before acknowledgement can replay the notification,
so the resumed host must read current task state. With `--session`, nothing
coordinates with an independently running interactive client: keep that client
stopped while the supervisor owns the session. It is not an always-on cloud
scheduler, and it does not implement ChatGPT Work's separate MCP Events
subscription API.

## Submit the actual change

Use a commit from **before the task began**, including work the host has already
committed. New files must be staged so Git includes them:

```sh
python3 .raven/host.py finish --task TASK_ID --base STARTING_COMMIT_SHA \
  --checks 'python -m pytest tests/test_retry.py: 42 passed'
```

This captures native Git diff bytes, disables external diff/text conversion,
and submits the matching SHA-256. It refuses untracked files and undecodable
bytes. It does not prove the chosen base is the right one, or that the supplied
checks actually ran. Tests and code review remain necessary; an agent can still
call MCP directly, so this is not a deployment control.

`python3 .raven/host.py status` checks the connection without sending a task.
Neither command prints the adapter's credential.

## Test coverage

Lifecycle tests execute the installed Python hook in a separate process and exercise real local HTTP MCP transport, exact prompt binding,
retries, reopen, moved checkout, principal isolation, leases, failures, late
corrections and native diff capture. Supervisor tests substitute the coding
process to test failures deterministically. Those tests do not establish that
every installed Claude or Codex release loads hooks or resumes identically.

Request attribution follows the authenticated agent credential. Give each
developer their own limited credential when you need personal attribution.
Copying one shared setup token to several developers identifies that shared
credential, not which human typed the prompt. Host-reported tool inventories
and session IDs are observations, not independent proof of a person's identity.
