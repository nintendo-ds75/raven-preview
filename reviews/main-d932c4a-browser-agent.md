# Browser tasks, built and exercised live

Base: `d932c4a9c8ed8f35c8a7cde5975bc14e1a44b842`.

Raven can now run a coding task from its own browser conversation. The browser
does not need local filesystem access: the Raven server snapshots an
operator-configured repository and runs its own agent through Anthropic's
Messages API. No Claude Code process, terminal bridge or external MCP client is
involved. The existing MCP workflow remains available.

The interface starts with a task composer and repository selector. Inside a
task, the conversation sits beside an optional overview of recorded decisions,
answers, approval status, investigation findings and people involved. Full
history retains source and contact details. Activity expands to actual tool
inputs and outputs. Follow-up messages, pause, continue and patch download work
in the browser. Desktop and phone layouts were exercised with computer use.

## Real task

Repository: [Pallets ItsDangerous](https://github.com/pallets/itsdangerous), frozen
at `672971d66a2ef9f85151e53283113f33d642dabd`.

Request entered in the actual browser:

> Allow a small, opt-in clock-skew leeway when checking timed signatures. Keep
> the current behavior by default. Work out the API and edge cases, ask the
> security owner to settle the policy, implement it, document it, and run the tests.

The built-in agent used `claude-sonnet-4-6` with real inference. It discovered
the files, investigated the API, registered decisions, read corrections, edited
a private copy, ran real commands in isolated containers and presented a patch.
The operator simulated **Mira Security**, a fictional owner, through the real
web inbox. This was a browser execution test with a seeded authority map, not a
cold-start routing benchmark. No public maintainer was contacted. No real Slack
workspace, Jira installation or GitHub write was exercised in this run.

| Step | Observed result |
| --- | --- |
| Start from prose | Durable task receipt; paths discovered by the agent |
| Investigate | Code reads and background discovery review recorded |
| Ask for judgment | Seven decisions, including two uncovered during finish discovery |
| Owner replies | Actual inbox signatures and two corrections; all seven authorized |
| Wake up | Reply resumed the agent without a browser message or model polling |
| Pause and restart | Saved conversation, patch and approvals survived server restarts |
| Implement | Code, tests, API documentation and changelog changed in the private copy |
| Test | 297 baseline tests; 330 tests on the resulting patch |
| Independent checks | Seven additional acceptance tests and the 330 project tests passed on a fresh copy of the downloaded patch |
| Finish | Agent received the advisory report before presenting its final summary |
| Download | Browser download hash matched the independently tested patch |

The independent tests cover unchanged defaults, inclusive old/future boundaries,
invalid types and negative values, the no-expiry case, error propagation and
serializer forwarding. The source checkout remained clean.

## What this run fixed

* A completion result originally stopped the agent before it received the
  report. Finish now has two steps: read the report, then present a result after
  rechecking the current patch and approval gate. A changed decision or patch
  cannot reuse an old presentation acknowledgment.
* Incomplete advisory readings initially produced an ambiguous conversation
  ending. They now pause with the work saved. After continuation, the agent can
  explicitly acknowledge the limitation and offer the patch for manual review.
  The server and UI add the warning independently of the agent's prose.
* Finishing discovery originally asked the model to transcribe the entire diff.
  The server now attaches the actual patch, saving output tokens and avoiding
  truncated or incomplete transcription.
* A completed review could race with the wait subscription. Waiting now uses
  one current state snapshot, and already-completed work does not put the agent
  to sleep. Replies arriving during a final call are not stranded.
* The UI now clears sent drafts, preserves text/focus/disclosures through polls,
  attributes shared replies and controls, and avoids late navigation after a
  request finishes. The phone overview switches views without horizontal overflow.
* Git snapshots preserve deliberately tracked files that also match ignore
  rules. File tools reject case variants of `.git` as well as path traversal and
  symlinks. Commands never fall back to the server shell.
* Provider prompt caching is enabled; actual cache-read usage was observed and
  saved. It is separate from decision memory and grants no authority.

## Validation

The full offline suite passed **2,387 tests, with 118 skipped** before the final
completion and attribution additions. The final browser-agent contract suite
passed **27 tests on SQLite and the same 27 on PostgreSQL**. All eight DOM
suites passed, including nine new browser-task scenarios. These are DOM/API
doubles; the real desktop and 390-pixel phone walkthrough was separate.

The production image and optional Docker CLI overlay were built. A server
inside Docker launched the isolated test runner and passed the project's 297
baseline tests. Isolation probes confirmed non-root execution, no external
network, a read-only root, no server credentials/database/socket in the command
container, and persistence of allowed task-file changes. The existing customer
deployment was not replaced.

## Remaining limits

The existing advisory reviewer remained incomplete in this live run. It also
flagged explicitly authorized behavior as a departure and speculated about code
outside its diff. Passing tests do not turn those readings into successful
reviews. The result is a patch for manual review, not a claim of verification.
The agent's preserved final summary calls these failures timeouts; the evidence
only establishes incomplete readings, not a timeout for every attempt. Treat
that diagnosis as an agent claim. Improving the reviewer and grounding failure
explanations remain separate work.

The operator still configures repositories, inference and the project's test
image once. Snapshots currently cap at 16 MiB, 4,000 files and 1 MiB per file,
and refuse symlinks and submodules. Only committed HEAD is copied. The runner
is serial and pauses after 60 model calls per continuation. Long conversations
still grow in cost and size despite provider caching. This trial included
operator continuations and several reloads while fixing the implementation;
it is not a claim that every task finishes unattended.

The opt-in Docker overlay gives the trusted server Docker daemon access and
runs it as root, so use a dedicated Raven host. Generated commands receive no
daemon access or host mounts. The browser returns a patch; branch creation,
push, merge and deployment are not implemented in this workflow.

[Setup instructions](../docs/browser-tasks.md) and
[sanitized evidence](../evals/newdev/results/browser-agent-2026-10-12/manifest.json)
include the generated patch, independent tests, unedited final summary and UI
screenshots. The full live database and transcript were kept outside the public
repository. No operator inference credentials were added to these artifacts.
