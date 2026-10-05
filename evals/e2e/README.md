# End-to-end evaluation

What is being sold is a loop, not a retrieval score: an agent kicks off a task, the decisions inside it reach the people who own them, the answers come back, the change follows the answers, nothing ships on an answer nobody signed. This runner puts the same tasks through three arms and scores what happens to the decisions inside them.

| arm | what runs |
| --- | --- |
| `host-alone` | the agent decides everything itself: each decision's `default` |
| `routing-only` | every decision goes to whoever the authority map or git names; no memory, no rules, no sign-off gate |
| `bridge` | the whole protocol over MCP: kickoff, nodes through the ladder, the wait, the finish gate |

People are simulated from each task's answer key: the owner answers or signs when asked; anyone else says it is not theirs and hands it on to the owner, who then does. That is the pilot's own workflow with the humans replaced by the key, which is what makes the arms comparable and repeatable.

## The task file

`tasks.example.json` is the shape. `org` is the organization: people (name, email, Slack id), the authority map, the coordinator, its settings (the example turns `auto_rules` on; Raven's default keeps every decision request-specific), and `prior`: decisions already signed before the tasks start, optionally as a rule (`{"conditions": "...", "expires": ""}`). Each task has a title, the requester, the paths, its decisions (`question`, `context`, `paths`, the agent's `default`, and the `key`: `owner`, `answer`, `settled`), and `action.expected`: the answers joined with ` | ` in decision order, which is what the agent's change must follow.

`settled: true` means the organization had already decided it (the prior list holds the decision); asking anyone is then an interruption unless a rule covers it. `default` is what the agent would do on its own.

## Scores

Per task and arm:

- `discovered`: decisions on the record before the agent acted
- `asks`: people asked; `interruptions`: asks about settled decisions
- `first_contact`: asks that reached the key's owner first (misroutes are recovered by a hand-on, and counted here)
- `authorized`: the agent acted only on authorized answers (the finish gate held)
- `adherence`: the agent's action equals `action.expected`
- `lost`: an answer given was not on the tree when the agent read it

The summary reports the rates per arm; `results.json` keeps every outcome.

## Running

```sh
python3 -m evals.e2e.run --tasks evals/e2e/tasks.example.json --out evals/e2e/results/example
python3 -m evals.e2e.run --tasks /path/to/pilot-tasks.json --out evals/e2e/results/pilot-week-1
```

**What this harness does not establish.** It is a deterministic protocol simulation, not a coding agent. Every decision is supplied to it from the task file, so `discovered` counts decisions the file already named: it measures what the protocol does with a decision, never whether a real agent notices one. The host-alone arm consumes the file's `default`s and the routing-only arm the key's answers; neither writes code. What the pilot still has to learn from real work with a real host agent: whether it registers the decisions it should, asks at a useful moment, keeps doing independent work, resumes after a human delay, and changes the code in line with the signed answer. Run those tasks with the hidden decision list withheld from the agent, have the owners label the decisions and the authority beforehand, and grade missed decisions as well as unnecessary asks.

`evals/real_oss` does exactly that on three held-out tasks from a real repository, with the real CLI as the host and the decision list withheld. Three tasks is not a pilot, and what it found is in its README; read the two together.

No model key is used; the deterministic rungs run. For a pilot: thirty to fifty tasks written from the team's real workflow with the original problem statements, the answer key written by the owners before the run, two labelers grading `adherence` by hand where an action is not a literal string, and the arms compared on the same file. A shadow pilot on live work, where Raven routes and records but authorizes nothing, comes before Raven is allowed to authorize anything; the gates are agreed with the pilot owners, not claimed.

`tests/test_e2e_eval.py` runs the example file under all three arms on every test run.
