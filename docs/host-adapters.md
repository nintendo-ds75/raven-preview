# Reliable task registration and optional background resume

MCP gives the coding agent Raven's tools. A host hook registers the task before
the model starts working, so kickoff no longer depends on the model remembering
an instruction. The agent still has to inspect the repository and discover
which decisions need people.

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

## Resume an idle coding session after a person replies

Raven keeps a durable mailbox for the authenticated host session. Human answers,
signatures, referrals, required follow-ups and source changes produce resumable
work. A late correction can wake a completed task too. Only one Raven supervisor
can lease a message at a time; acknowledgement advances its cursor.

Stop the interactive host first, then deliberately enable a local supervisor:

```sh
cd /absolute/path/to/code
python3 .raven/host.py watch --host claude --session EXACT_SESSION_ID --allow-background
# Or use --host codex with that host's session ID.
```

The supervisor polls every 15 seconds and invokes the installed host's normal
resume command, in the bound checkout, with its normal permissions. Source text
cannot specify a shell command. Ctrl-C stops background execution. Raven's server
never starts a process on the customer's machine by itself.

Delivery is **at least once**. A failed process releases the message; an abandoned
lease can be claimed after 30 minutes. One invocation is bounded to 20 minutes.
A crash after host success but before acknowledgement can replay the notification,
so the resumed host must read current task state. This does not coordinate with
an independently running interactive client: keep that client stopped while the
supervisor owns the session. It is not an always-on cloud scheduler, and it does
not implement ChatGPT Work's separate MCP Events subscription API.

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
