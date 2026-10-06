# The first hour

A deterministic first-hour protocol walkthrough against a real Git checkout
and isolated Raven instances. A scripted host speaks MCP through a stdio
subprocess and HTTP. Simulated people answer through Raven's Slack delivery
handler, with a fake transport recording outbound messages. The harness prepares
a synthetic authority map to exercise explicit ownership. This is not a real
coding-model run, a fresh Docker installation, or a real Slack installation, and
that map is not a product setup requirement.

```sh
python3 -m evals.newdev.walkthrough                      # this repository
python3 -m evals.newdev.walkthrough --repo /path/to/repo --out result.json
```

It prints `ok` or `FAIL` for each promise with what happened instead,
and exits non-zero if any failed. Everything it writes is a disposable
database under `--work`.

## What it checks, in the order you meet it

1. **Ingest** reads a real checkout and says what it found.
2. **Readiness** reports what is still missing before you trust it, and
   every finding says what to do. It is in the state the inbox polls, so
   you cannot miss it.
3. **The harness's explicit authority map** clears the deterministic scenario's blockers.
4. **The MCP server** starts, exposes the expected agent-tool set, and its instructions say
   what to do first.
5. **Kickoff** engages a task with a real judgment call in it, says why
   in terms you can check, and names the decisions the task may contain
   before you have asked it anything, each with the signal it came from.
6. **Nodes** route to a person you put in the map, with evidence naming
   the signal; a child hangs off the decision it grew from; nothing is
   authorized yet.
7. **The person's message** carries the question, the context and how to
   reply; two decisions make two messages, not eleven; and nothing is
   described as coming from your records unless it did.
8. **An explicit simulated Slack answer** is recorded and attributed; a
   non-answer does not become a decision. Natural-language inference is covered
   by separate conversation tests, not this command-driven check.
9. **The gate** refuses the finish while a decision waits on a person,
   naming it.
10. **A killed process** started again gets the same task back by its
    `client_key`, with the answer on the tree and what to do next.
11. **Finishing** says plainly that it authorized decisions and did not
    verify the change, records what you said you ran as your claim, and
    lists who authorized what.
12. **The next task** reuses the answer a person gave, cites where it
    came from, still wants a signature, and goes back to the person who
    gave it.
13. **An area nobody owns** still engages, and the reason says nobody is
    known rather than that nothing is needed.

## Where it came from

For automatic directory discovery without a prepared authority map, natural
conversation, actual coding-model runs and the boundary between simulated and
live integrations, see [the verification reports](../../docs/verification.md)
and [the October 5 customer loop](../../docs/customer-loop-2026-10-05.md).

A reviewer ran a real Codex host against a frozen Grafana checkout and
reported ten steps with what held and what did not: kickoff passing a
task because no owner was known, a prediction presented in Slack as
coming "from the records", three decisions producing eleven messages, a
reply refused as stale because the node had just been handed on, and a
finish that read as though the implementation had been verified. Each of
those is a line in this walkthrough now, so the next run says whether it
still holds rather than needing somebody to notice again.

Running it against a large repository is worth doing: the first Grafana
run failed on reuse going back to the wrong owner, and the reason was
that CODEOWNERS entries had been added as people. A team is not somebody
a question can be put to; `add_person` refuses a team handle now and
readiness reports any that are already stored.
