# The first hour

What a new engineer does on day one, run for real and checked promise by
promise. Not a unit test: the commands from the README in the order a
person runs them, against a real git checkout, with the agent side
speaking MCP to a real Bridge (a stdio subprocess, and then a shared
Bridge over HTTP the way a team runs it) and the human side replying
through the same Slack event handler Slack posts to.

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
3. **Setting the map up** clears the blockers.
4. **The MCP server** starts, offers ten tools, and its instructions say
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
8. **A Slack reply** stated as an answer is recorded and attributed; a
   conversational one never becomes a decision.
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
