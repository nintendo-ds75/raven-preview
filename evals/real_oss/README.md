# A real agent, a real repository, real people

`evals/e2e` is a protocol simulation: the decisions are handed to it by
the task file, so it measures what Raven does with a decision and never
whether an agent notices one. Its own README says what a real-agent
evaluation would have to establish instead — whether the agent registers
the decisions it should, asks at a useful moment, keeps working, resumes
after a human delay, and changes the code in line with the signed
answer. This is that evaluation, on three held-out tasks.

The host is the Claude Code CLI in print mode, reaching Raven over MCP
stdio. It is given the problem and nothing else: no decision list, no
owner names, no answer key, and no sight of the change that really
landed. Each task runs twice, and each is also run with no Raven at all
so the two can be compared.

## Where the tasks come from

A frozen checkout of `grafana/grafana`, cut in two by a date.

**Before the cutoff** is everything Raven is given: the git history and
blame at the cutoff commit, the CODEOWNERS file as it stood there, and
an authority map an operator would have written that day. Ingest is
scoped to `pkg/services` and `pkg/api`.

**After the cutoff** is the answer key, and Raven never sees it: three
real merged changes, the decisions inside them, what the project in fact
chose, and who in fact chose it.

Each task's brief is the problem as its requester would have put it,
written by reading the real change's own motivation with the solution
taken out. The diff is never shown to the agent, and the working copy is
the repository as it stood at the commit just before the change landed.

| task | the real change | landed | key owner |
| --- | --- | --- | --- |
| `toggle-handling` | [a89e4000](https://github.com/grafana/grafana/commit/a89e40005f118d6288611b33f6073f7f316c5f7c) a toggle controlling how the frontend handles reads of the legacy `config.featureToggles` map | 2026-09-16 | Engineer 419848 |
| `smallint-overflow` | [24a0b5be](https://github.com/grafana/grafana/commit/24a0b5be4da0ca1e1ff2ff0e073bd74b656307a6) widening `missing_series_evals_to_resolve` past the SMALLINT limit | 2026-09-18 | Engineer e13be7 |
| `retire-toggle` | [35bf13f0](https://github.com/grafana/grafana/commit/35bf13f080ae7bd06aaa567582d6bb17b24a8eee) retiring the `improvedExternalSessionHandling` toggle | 2026-09-07 | Engineer 61971d |

**The authority map is the operator's setup step, and it is a proxy.**
CODEOWNERS in a repository this size names teams, not people, and a team
is not somebody you can ask. Expanding a team into people is what an
operator does on day one of a pilot; here it is done mechanically, from
who authored changes under that team's paths **before the cutoff**, three
commits or more, generated files excluded. Nothing from the held-out
window reaches it. It is a rough map: `pkg/services/featuremgmt` is
touched by 52 people who clear that bar, because every feature-flag
change in the product passes through it. Read `team_contact` against
`acceptable_size`, and read `first_contact` as the measure that means
something.

**The key owner is the person who in fact made the call, which is not
the same as the person with the authority to make it.** `bench/routing`
carries the same caveat and for the same reason: this is what a held-out
git history can tell you, not a labelled ground truth from the team.

## What each run does

1. A worktree of the repository at the commit before the real change.
2. A fresh copy of the ingested database, and an MCP config pointing the
   host at `bridge_mcp.py` over stdio.
3. **Phase one.** The host gets the brief and the house rule (the
   paragraph a pilot puts in its own instructions; it names no decision,
   owner or answer). Nobody replies during this phase: it ends with
   whatever the host did before any human was available.
4. The canvas is recorded, and the finish gate is probed and put back.
5. **The owners act**, through the same permission check a browser or
   Slack goes through: the person Raven routed to answers if the key
   says the decision is theirs, and hands it on to the person it belongs
   to if it is not. A node Raven resolved from the record is signed if
   it already says what the key says, and corrected if it does not.
6. **Phase two.** A *new* CLI process, with no memory of the first, is
   told only to pick the task up by its `client_key`. Everything it knows
   about what people decided, it gets from Raven.
7. The gate is checked again, and the diff is read.

The `alone` arm is the same brief with no Raven and one phase.

The simulated owners are the one thing here that is not real, and it is
the same simplification `evals/e2e` makes.

## What is scored

Read off the canvas and the transcript: `kickoff_first` (Raven was
called before any work), `registered` (decisions written to the canvas),
`routed`, `first_contact` and `team_contact`, `settled_alone` (Raven
answered it from the record and asked for a signature), `handed_on`,
`corrected`, `gate_held` (the finish was refused while a decision waited
— null when nothing was on the canvas to hold), `resumed` and
`saw_answers` (phase two recovered the same task and read the answers),
and `authorized` (nothing blocking was left unauthorized at the end).

`followed` is read off the **diff**, not off what the agent said it did:
each task names strings that have to appear on added lines, on removed
lines, or nowhere, and the files that have to be touched. The first run
of this evaluation graded the agent's closing summary, which let a run
that described the right change but did not make it count as a pass.
Fixing that is what turned up the most interesting result below. The
key answers themselves were written before any run and have not changed.

After the owners have answered and the second phase is done, a **third
host** gets a sibling task on the same ground, with a brief that names
no earlier decision, and `followup_recalled` counts its nodes that came
back carrying the answer a person gave on the first task.

## Decision coverage: `coverage.py`

The reviewer's largest remaining gap is that Raven cannot route a
decision the agent never writes down, and `bridge_start_task` answers it
with `candidates`. Nothing measured whether they are the right ones.
`coverage.py` asks how many of the decisions each held-out change in
fact turned on are named before any agent has looked at anything, and
how many candidates are named that no real decision corresponds to.

```sh
python3 -m evals.real_oss.coverage                                  # templates only
BRIDGE_MODEL_API=claude-cli python3 -m evals.real_oss.coverage --semantic
```

| | real decisions named | candidates | matching none |
| --- | --- | --- | --- |
| templates only | 2 of 8 | 8 | 5 |
| read closely, three runs | **4, 6 and 4 of 8** | 12 to 15 | 5 to 8 |

The templates stand for Raven's own signals (a judgment word in the
task, a prior decision, areas listed to different people) and are
general by construction: "is this a schema change that has to be
migrated" where the decision was "which tables does the migration have
to cover". Reading the task names the specific ones, at the cost of
roughly twice as many candidates to dismiss. A candidate is a prompt and
not a decision, so a wrong one costs the agent one read, which is why
this panel reports and never fails.

Two things to hold on to when quoting it. The model is not
deterministic: 4, 6 and 4 across three runs of the same input, so quote
the range. And the matcher undercounts, because each decision's
identifying words were written to recognise its **answer**, not a
question about it: "should the code paths that only ran when the toggle
was disabled be deleted" is the decision "which behaviour survives the
removal" and scores zero against its terms. The panel prints the nearest
decision and its score for every unmatched candidate so a reader can see
which misses are real.

## Without an agent: `probes.py`

An agent that never asks a question tells you nothing about routing or
memory, and a real agent asks what it feels like asking. `probes.py`
puts the questions itself, so the two things Raven does on its own are
measured without that variance.

**Routing.** For each area CODEOWNERS names, ask who decides. A good
answer names a person, says which signal named them, and that person is
on the team the repository itself lists there. It is a sanity check, not
an accuracy score; `bench/routing` is the accuracy measurement.

**Memory.** A person answers one question, and then five more go in:
the same question on a new task, a paraphrase, the same words about
another area, something unrelated, and who decides about the area. What
should happen: the first two come back carrying that answer with its
citation and still wanting a signature, the third comes back a
prediction that says out loud it was given for another scope, the
fourth reaches nobody's memory, and the fifth resolves from the
ownership graph. `recalled` and `as_evidence` are reported apart: a
direct hit resolves, a near one is offered as a prediction to confirm,
and both are the answer reaching the new question.

```sh
python3 -m evals.real_oss.probes --out .../probes.json            # deterministic rungs
python3 -m evals.real_oss.probes --out .../probes.json --semantic # with a model backend
```

## What the recorded run found

Two recorded runs, a day apart, on the same three tasks. Read them as
transcripts, not as rates: three tasks is not a pilot, and a real agent
is not deterministic.

### 2026-09-23, the whole stack with the model rungs on

`results/grafana-2026-09-23-inference/results.json`, Claude Code 2.1.280,
**model rungs on** through the claude CLI: kickoff triage, the decisions
named by reading the task, the memory rerank, the owner's brief and the
record selector all live. Beside it, the same three tasks with the rungs
off, from two days before.

| | rungs off | rungs on |
| --- | --- | --- |
| registered a decision | 3 | **4** |
| routed it to a person | 3 | **4** |
| contacted somebody on the listed team | 2 | **4** |
| the gate held | 3 of 3 | 3 of 3 |
| finished with every decision authorized | 3 of 3 | 3 of 3 |
| the sibling task recalled the answer | 0 | **1** |
| the diff matched what landed upstream | 2 of 3 | 1 of 3 |
| reached the person who really made the call | 0 | 0 |

Read the last two rows carefully. `followed` is one sample per task of a
non-deterministic agent, and the task that changed did so because its
agent put the migration in a different file than the one upstream used,
not because anything in Raven moved. Three tasks, one run each, is a
transcript.

**The conformance report barely fired, and that is the finding.** A host
supplied a diff on 1 of the 3 tasks, and truncated it to 300 characters
of an unrelated snapshot file. The tool description alone does not get a
real agent to hand over its change, so it now says the whole diff and
that a fragment is worse than nothing, and the harness passes the diff
it already computes rather than leaving it to the host: leaving it there
measured the agent's diligence and not the report.

Read offline against the three real diffs, the report says `follows` on
three of the four signed decisions and `unclear` on the fourth. It
disagrees with the `followed` column twice, and both times the report is
right about the question it was asked. On `smallint-overflow` the change
widens both columns to BIGINT through a migration, which is what the
owner signed; `followed` is false because upstream put that migration in
`migrations.go` and this agent did not. The two measure different
things, which is why `evals/newdev/conformance.py` has ground truth of
its own.

### 2026-09-21, after the deployment and discovery fixes

`results/grafana-2026-09-21-repo-scope/results.json`, same three tasks,
same host, **model rungs off** (this run had no key, so it is the
deterministic arm and not a like-for-like successor to the run below).
Run after the kickoff stopped reading its verdict from the host's own
boilerplate, started treating a new operator-visible switch as somebody's
call, and started saying when a task named a repository Raven has never
read:

| | model rungs on, before | deterministic, now |
| --- | --- | --- |
| kickoff engaged | 2 of 3 | **3 of 3** |
| registered a decision in phase one | 2 of 3 | **3 of 3** |
| routed it to a person | 1 of 3 | **3 of 3** |
| the gate held | 2 of 2 | **3 of 3** |
| finished with every decision authorized | 2 of 3 | **3 of 3** |
| **the diff followed the signed answer** | 1 of 3 | **2 of 3** |
| the sibling task recalled the owner's answer | 2 of 2 | 0 of 3 |
| reached the person who really made the call | 0 | 0 |

Two things this run found that the reviewer's report did not. A host
passed its own worktree path as `repo` on two of the three tasks; that
matched no graph, so discovery read nothing and the verdict was a
confident pass that sounded like a judgment about the work. The kickoff
says so now. And when the host took that advice and called again with
`grafana/grafana` — same title, same goal, same `client_key` — the key
rule refused it as renaming a task. Correcting an identity that matched
no graph is not renaming; that call is taken now, and `retire-toggle`
goes from registering nothing to running the whole loop with a diff that
matches what was signed.

Sibling recall went the other way and the reason is the arm, not a
regression: all three followups engaged and registered (they did not
before), and then the deterministic memory rung declined to reuse. On
`smallint-overflow` it declined correctly and said why — "best match
scored 0.50, under the 0.6 floor" — because the earlier answer was about
`missing_series_evals_to_resolve` and the new question is about
`keep_firing_for`, a different column. Paraphrase reuse is what the model
rung is for; `probes-deterministic.json` measures the same ground without
an agent and scores 14 of 15.

`first_contact` has still not moved. On a CODEOWNERS team of 143 people,
with public history and no operator-recorded decider, naming the one
person who made the call is a guess, and Raven names a team member with
its evidence instead of claiming otherwise.

#### Why reranking the candidates cannot move `first_contact`

Worth writing down, because the obvious next idea does not work on this
fixture and somebody will try it. The plan was to let a model pick the
decider out of the deterministic candidate set. Measured first:

| task | who in fact decided | in the top 6 candidates | rank among authors of the paths |
| --- | --- | --- | --- |
| toggle-handling | Engineer 419848 | no | not in the top 8 |
| smallint-overflow | Engineer e13be7 | no | not in the top 8 |
| retire-toggle | Engineer 61971d | no | not in the top 8 |

All three hold a `decides` row for the right area, because the fixture
expands the CODEOWNERS team into its members. All three are outside the
top 6 that routing surfaces, and none of them is in the top 8 authors of
the paths their own change touched: their pre-cutoff commits (20, 67 and
122 of them) are elsewhere in the repository. A model that selects from
a candidate set cannot name somebody the set does not contain, and
widening the set by history does not reach them either.

That is a limit of what pre-cutoff history knows, not of the ranking. The
person who picks up work in an area they have not worked in before leaves
no trace that says so beforehand, and that is an ordinary way for work to
happen. Raven's answer to not knowing is to name a candidate with its
evidence and let them hand it on, which is what these runs do: 3 of 3
routed, 3 of 3 answered after a handoff.

What did come out of measuring it: with 139 people holding identical
authority for one area and their scores within 0.02 of each other, the
winner's evidence read like a finding about that person. It now says how
many share that authority, so the name reads as a placement and gets
handed on rather than trusted.

### 2026-09-21, after the assisted-pilot fixes

`results/grafana-2026-09-21-candidates/results.json`, same three tasks,
same host, model rungs on, after `bridge_start_task` began returning
candidates, asking for the task as it was given, and refusing to let the
model lower an engage verdict:

| | before | after |
| --- | --- | --- |
| kickoff engaged | 0 of 3 | **2 of 3** |
| registered a decision in phase one | 0 of 3 | **2 of 3** |
| the gate held where there was something to hold | n/a | 2 of 2 |
| the sibling task recalled the owner's answer | 0 of 1 | **2 of 2** |
| reached the person who really made the call | 0 | 0 |

The two that registered ran the whole loop: routed or placed from the
unrouted queue, answered, the finish refused until then, and the sibling
task's node carrying the earlier answer with its citation. The third
registered nothing again. `first_contact` has not moved.

### 2026-09-21, and what Raven does on its own

`results/grafana-2026-09-21/`, Claude Code 2.1.278, model rungs on.

**Who to ask** (`probes-deterministic.json`, `probes-semantic.json`).
Six areas, six questions, no agent involved:

| area | named | on the CODEOWNERS team | cites CODEOWNERS | cites history |
| --- | --- | --- | --- | --- |
| `pkg/services/featuremgmt` | Engineer 24f6f6 | yes, of 52 | yes | yes |
| `pkg/services/ngalert` | Engineer 8bac4a | yes, of 12 | yes | yes |
| `pkg/services/sqlstore/migrations/ualert` | Engineer d81766 | yes, of 12 | yes | yes |
| `pkg/services/authn` | Engineer 9fb8e8 | yes, of 7 | yes | yes |
| `pkg/services/oauthtoken` | Engineer 33a714 | yes, of 7 | yes | yes |
| `pkg/services/sqlstore/migrations` | Engineer 320955 | yes, of 2 | yes | yes |

Six of six named a real person on the team the repository lists for that
path, each with an evidence line that quotes the CODEOWNERS entry and
the person's own history under it. What this does not tell you is
whether that person is the *right* one of the fifty-two; that is what
`first_contact` in the agent run measures, and it is still 0.

**Memory, after a person answers.** Fifteen probes, five per task:

| | deterministic | model rungs on |
| --- | --- | --- |
| behaved as the contract says | 13 of 15 | 14 of 15 |
| the answer reached a later question | 4 of 6 | 5 of 6 |
| reached it as direct evidence | 4 | 4 |

The same question on a new task came back resolved from that answer,
citing the decision, and still blocking for a signature: every time, in
both arms. The same words about another area came back a prediction
whose evidence says which scope it was given for, never as evidence for
this one. An unrelated question reached nobody's memory. Who-decides
resolved from the ownership graph.

What misses is a distant paraphrase. Retrieval here is lexical plus a
hashed embedding, so "Do we change the column type, or validate in the
application?" scored 0.58 against a 0.60 floor for an answer to "Fix
this in the schema or in the application?", and a paraphrase of a
four-word question scored 0.27. Both missed honestly: they said the
score and the floor and routed to a person. With the model rungs on,
one of the two is recalled instead, offered as a prediction to confirm.
A signed answer is never authorization for a new question in either arm.

**With the agent** (`results.json`), three tasks, phase one, phase two,
and a sibling task each:

| task | goal passed | kickoff | registered | gate held | corrected | sibling recalled |
| --- | --- | --- | --- | --- | --- | --- |
| `toggle-handling` | yes | pass | 1 | yes | 1 | **yes** |
| `smallint-overflow` | yes | **engage** | 0 | n/a | — | no |
| `retire-toggle` | yes | pass | 0 | n/a | — | no |

`toggle-handling` is the whole loop on one task, and the part that was
being tested here works: the agent registered a decision, Raven
resolved it from the record and could not route it, the operator
assigned it from the unrouted queue, the owner corrected the answer, the
finish was refused until they had, and then the **sibling task's** node
came back citing that decision by id, in the agent's own words: "Directly
mirrors the already-signed sibling decision (node 496146115ac5)". An
answer a person gave on one task reached a different task, through a
host process that never saw the first one.

The other two registered nothing, and that is the open problem. Across
today and yesterday the agent wrote a decision on three of six runs.
What Raven gets to route is whatever the agent chooses to write down,
and the house rule in `HOUSE_RULE` does not make it choose. Passing the
requester's own wording helps the triage — `smallint-overflow` engaged
here and passed in the run before, on the same task, because the agent
sent a goal this time instead of a title — but engaging is not the same
as the agent asking. Making Raven propose the decisions it can see in
the digest, rather than waiting to be told, is the obvious next thing to
try, and it is a design question rather than a bug.

### 2026-09-20, the full loop with both arms

`results/grafana-2026-09-20/results.json`, Claude Code 2.1.278, cutoff
`2d289f63d515`, head `39703a2c01e3`, deterministic rungs.

| task | arm | kickoff first | registered | routed | first contact | team contact | gate held | resumed | followed |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `toggle-handling` | bridge | yes | 1 | 1 | 0 | 1 of 52 | yes | yes | **no** |
| `toggle-handling` | alone | — | 0 | — | — | — | — | — | no |
| `smallint-overflow` | bridge | yes | 1 | 1 | 0 | 1 of 12 | yes | yes | **no** |
| `smallint-overflow` | alone | — | 0 | — | — | — | — | — | yes |
| `retire-toggle` | bridge | yes | **0** | — | — | — | n/a | yes | yes |
| `retire-toggle` | alone | — | 0 | — | — | — | — | — | yes |

The loop itself held. The host called `bridge_start_task` before doing
any work on all three tasks. Where it wrote a decision to the canvas,
Raven routed it to a real person with real evidence, the finish was
refused while that person had not acted, a fresh process with no memory
of the first recovered the same task by its `client_key` and read the
answers off the tree, and the task finished with nothing blocking
unauthorized. Two runs, two phases each, about 13 minutes and $3.50 for
the Raven arm against 7 minutes and $1.62 for the arm with no Raven.

Four things did not hold, and they are the point of running this.

**A signature does not cover the diff.** On `toggle-handling`, Raven
resolved the node from the record, the owner *corrected* the answer to
the three-state `off`/`log`/`block` shape the project really chose, the
second process read the correction off the tree, reported that its
change matched it, and finished with the gate satisfied. The registry
entry it left behind is `Expression: "false"`: a boolean, with no `log`
and no `block` anywhere in the diff. Raven gated authorization and got
it right; nothing in Raven checks that the code does what was signed,
and the agent's own summary is not evidence that it does. This is the
single most useful thing this evaluation has produced, and it is now
said plainly in `README.md` and `PILOT.md`.

**Routing reached the team, never the person.** Both routed nodes went
to someone on the CODEOWNERS team that owns the file and neither went to
the person who in fact made the call. In both cases the hand-on
recovered it in one hop, which is the designed recovery, and the
`first_contact` column is the honest score.

**A decision nobody writes down is a decision Raven cannot route.**
`retire-toggle` — removing a feature toggle, which carries a real
question about operators who had it switched off — produced no node at
all, on this run and on both earlier ones. The kickoff verdict was
`pass` and the agent did the whole removal itself, correctly. Raven
adds nothing to a task the agent never asks about, and the house rule as
written did not make it ask.

**Raven did not make the change better.** One of three runs in each arm
matched the project's change on the strict diff check. The Raven arm
splits its budget across two phases and spends turns on the protocol, so
this is not a clean comparison of code quality, and three tasks would
not settle it if it were. What Raven changed here is who knew and what
is on the record, not what was written.

## What it found in Raven itself

Six defects across the two runs, each now fixed with a rejecting test.

From the 2026-09-21 run and its probes:

- **One `client_ref`, two nodes.** A `client_ref` is the agent's own
  name for a node and its promise is that writing it twice writes one
  node. It was checked against published nodes only, and the ref itself
  was not written until the end of the write, so a retry that overlapped
  the first call wrote a second node and both blocked the task. The ref
  is now claimed before the ladder runs, an overlapping write waits for
  the node the first one produces, and a claim left behind by a write
  that died is taken over after two minutes. (`bridge/graph.py`,
  `bridge/canvas.py`, `tests/test_canvas.py`, in process and over HTTP.)
  Found by the reviewer running concurrent MCP calls, not by this
  evaluation; fixed here.
- **"Who decides about X" was answered with a pull request title.** The
  accountability rung matched "who owns", "whose call" and "who is
  accountable", but not the present tense an agent actually writes, so
  the question fell through to the records rung and came back
  `resolved`, with a PR subject as the answer. A confident wrong answer
  to the one question the product exists for. (`bridge/ladder.py`,
  `tests/test_ladder.py`.)
- **The model triage silently downgraded engage verdicts.** With a key
  set, the fast model advises the kickoff verdict and could overrule it
  either way. On this repository it turned two of three `engage`
  verdicts into `pass`, calling a schema migration and a feature-toggle
  removal routine cleanup. The two mistakes are not the same size, so it
  may now raise a verdict and not lower one, and its dissent is recorded
  on the run. (`bridge/canvas.py`, `tests/test_canvas.py`.)
- **A pass formed from a one-line title did not say so.** The same task
  engages on the requester's wording and passes on the terse title an
  agent writes for itself, and the agent sent no `goal` at all on two of
  three tasks. The verdict now says when it was formed from a title
  alone, and `bridge_start_task` asks for the task as it was given. The
  agent then passed a goal on three of three, and one kickoff flipped
  from `pass` to `engage` on the same task as a result.

From the 2026-09-20 run:

- **A broad path authority outranked a narrow one.** CODEOWNERS' own
  rule is that the most specific matching pattern owns the file, and the
  authority map ignored that: `pkg/services/sqlstore/migrations/ualert/`
  belongs to the alerting squad, but the rule for the directory above it
  won and the node went to search-and-storage. A person named by several
  rules also had their weight summed, so being on four teams made
  somebody four times the owner. (`bridge/signals.py`,
  `tests/test_authority.py`.)
- **A node Raven could not route was a dead end.** A node the ladder
  answered and could not route waits on a person nobody named. It did
  not appear in the unrouted count, and `assign` refused it because it
  was not an open question, so there was no way to put it in front of
  anybody from the inbox. (`bridge/store.py`, `web/app.js`,
  `tests/test_canvas.py`.)
- The harness's own gate probe completed the task it was probing, which
  corrupted the second phase of a run. Fixed in `run_host.py`.

The runs that turned a defect up are not kept. Each time, the fix landed
and the whole evaluation was run again from scratch; what is recorded is
the last run in each case. Grading was corrected once after a run (the
golden migration-ids file, see `spec.py`) and those results were
regraded in place with `--rescore`, which runs no agents and touches no
transcripts.

Two things the harness itself does not do, which shape what the numbers
can say. The owners answer once, between the two phases, so a node the
agent registers on the resumed pass is never answered and the agent
waits on it. And a run is three tasks: every number here is a count out
of three or six, not a rate.

## Running it

```sh
git clone --filter=blob:none --no-checkout https://github.com/grafana/grafana /path/to/grafana
git -C /path/to/grafana sparse-checkout set pkg/services pkg/api .github
git -C /path/to/grafana checkout <a commit at or after the head below>

python3 -m evals.real_oss.fetch --repo /path/to/grafana
python3 -m evals.real_oss.probes   --out evals/real_oss/results/<name>/probes.json
python3 -m evals.real_oss.run_host --out evals/real_oss/results/<name> --work /tmp/real-oss
```

`--semantic` on either one lets Raven run its model-backed rungs, which
is the configuration a pilot with a key has; without it the deterministic
rungs run alone.

`fetch.py` writes `bridge.db` (git-ignored, disposable) and `tasks.json`
(committed, so a run can be repeated). `run_host.py` needs the `claude`
CLI on PATH and authenticated; checkouts, databases and the full
stream-json transcripts go under `--work`, and only `results.json` is
kept. `--only <task>` and `--arm bridge` narrow a run; `--rescore`
regrades the results already in `--out` against the current checks
without running anything. Every run records the host version, the cutoff
commit and the head it was cut from.

A real agent is not deterministic: the three runs behind the recorded
one differed in what they registered, where they put the migration and
whether they routed at all. Quote a run, not a rate, and keep the
`results.json` you are quoting.
