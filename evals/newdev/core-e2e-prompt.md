# Full end-to-end test of Raven core

You are testing Raven, not building it. Sync to `main`, record the exact revision you test, and freeze it for the
whole run. Do not change production code. If something blocks you, record it as a finding, work around it only in
the harness, and keep going.

## What this run must answer

Does Raven, used by a real coding agent and simulated people, do its core job end to end?

1. **Right person.** Every question reaches the person who actually decides it, and never the requester on
   inferred signals.
2. **Right questions, without annoying anyone.** People are asked only what they need to decide. They are asked
   once, clearly and faithfully. Nothing Raven could have settled is put to them.
3. **Resolve first.** What records and earlier answers already settle is resolved before anyone is asked, and
   reuse is exactly as broad as the earlier answer allows. That means none across a changed customer, release, path
   or expiry, and none from cancelled or superseded records.
4. **The rest of the loop.** The canvas, sign-off, required approvers, follow-ups, rules, corrections, waiting and
   resuming, the finish gates and diff reading, permissions, recovery and the UI.

## Ground rules

- **Environment:**
  - a fresh Docker workspace on PostgreSQL, with auth on and live inference on;
  - a real Claude Code host over HTTP MCP with an agent-only credential;
  - a real public repository checkout (urllib3 again is fine, since the harness exists: `live_user_host.py`,
    `connector_double.py`, `retry_after_jitter_oracle.py`);
  - GitHub history may be replayed, and Slack and Jira simulated. Say which parts were simulated everywhere you
    report a result.
- **People:**
  - synthetic only, with `.invalid` emails;
  - never contact or answer as a real maintainer;
  - no personal emails, tokens, cookies, CSRF values, passwords or API keys in committed evidence;
  - never paste a secret into chat.
- **Oracle first.** Before any host runs, write `oracle.json`. For every planned question it gives the expected
  owner, any required approver, and the expected ladder outcome (resolved, predicted, pending, unrouted,
  duplicate, rule-covered) with the reason. Score against it afterwards and do not edit it after the run starts.
- **Two drivers, labelled apart.**
  - A real host discovers decisions on its own. It gets the task, not a decision list and not owner names.
  - Controlled probes call MCP and the API directly with an agent token, to guarantee every shape below is
    exercised.
- **Budget.** Give each host run at least $10 and record the receipt, turns, wall time, wait calls and any idle
  aborts. Do not set `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT`; the default is part of the test.

## The organization to set up

Use the API or the UI, and record which for each step.

**People and roles**

| Person | Role |
|---|---|
| Mira Runtime | decides `src/urllib3/util/*` (path) and category `compat` |
| Theo Release | approves `src/urllib3/util/retry.py` (a required approver); decides `docs/*` and `changelog/*` |
| Sam Security | decides categories `security` and `auth` |
| Nora Docs | knows `docs/*` (knows, not decides) |
| Kai Pool | member of team `http-core` |
| Casey Coord | coordinator, for areas nobody is mapped to |
| Rae Requester | kicks off the tasks; must never be routed to on inferred signals |

**Team and map extras**

- Team `http-core` (Mira and Kai), named in a CODEOWNERS line for `src/urllib3/connectionpool.py`.
- One expired authority row, a negative control: someone who used to decide `src/urllib3/response.py`.

**Records (the static context), through `/api/records`**

- A settled decision record (Done).
- A cancelled proposal (like NET-102) that says the opposite of current policy.
- A record marked superseded by a newer one.
- An open ticket that proposes something nobody ratified.
- Two Slack messages captured with `record:`.
- Ordinary chatter, which must not become a record.

## Tasks

Run them in this order. Kick off every task with `bridge_start_task` before any other work, passing the full goal
and `facts` where the task states them.

**Task A: seed (real host, full loop).** "Add an opt-in `retry_after_jitter` to `Retry` for Retry-After waits",
`facts: release=library-next`.

Owners answer in the web inbox and in Slack. Give some answers applicability, written down in the oracle:

- validation: "negative or non-finite numeric Retry options raise ValueError; bools are accepted". Also make it a
  rule: `rule if numeric retry option`;
- docs: a changelog fragment of type `feature` plus the docstring, for `release=library-next`;
- the cap interaction: paths `src/urllib3/util/retry.py`;
- one customer-specific answer: `requires customer=acme`;
- one answer with an expiry that has already passed.

Add one **required** follow-up from the inbox and one optional one.

**Task B: reuse-heavy (real host).** "Add an opt-in `backoff_deadline` (a total time budget for retries) to
`Retry`", `facts: release=library-next`.

- Expect validation and docs to be settled from Task A. With `auto_rules` off they come as evidence wanting a
  signature; with `auto_rules` on they are covered by the rule.
- Expect the deadline's semantics, and whether a deadline may cut a server-requested Retry-After wait, to be new
  and routed. That includes Theo as required approver on the protected file.

**Task C: scope change (probes).** Backport to `release=1.26.x` for `customer=globex`. The docs and customer
answers from Task A must come back as predictions for another scope. The message must name both values, and
nothing may be authorized.

**Task D: trivial (real host).** "Fix a typo in `docs/user-guide.rst`." Expect `pass` and zero questions to anyone.

**Task E: cross-area (real host or probes).** "Log the Retry-After header value at debug level."

- The privacy or security question goes to Sam by category, not to whoever owns the file.
- Sam hands one sub-question on with `not me @Theo for docs`, and another with `not me @Theo just this one`.
- Check what each hand-on teaches, and that no one gains authority nobody chose.

## What to test, with the checks that count

### 1. Right person

Probe each shape. For each one, record the owner, the required approvers, the "Why you" line and the finish
refusal text.

- a verified path owner, a category owner, and a repo-wide fallback;
- a path inherited from the task, a path inherited from a parent node, and several explicit paths with the
  protected file second;
- a required approver: the finish is refused naming the missing reviewer, the inbox shows "Signed in part ·
  Waiting on …", and the task completes only after the reviewer signs;
- a CODEOWNERS team expanded to its members;
- the requester never chosen on inferred signals;
- an unknown area going to the coordinator, or unrouted with an honest reason;
- the expired authority not used;
- a repository Raven has not indexed, routed by the authority map alone;
- a route resting on an old GitHub sync saying so.

Score routing accuracy against the oracle.

### 2. Right questions, without annoying

- **Discovery:**
  - recall: decisions the oracle says need a person that the host did not register;
  - precision: questions it put to people that the oracle says were not needed.
- **Per person:** count every touch (a question, a sign-off request, a follow-up, a reminder) and compare with
  the oracle's minimum.
  - No second ask after an answer.
  - Two tasks asking the same open question make one ask, and one answer settles both.
  - Nothing that records or memory settle arrives as a question; it arrives as something to sign.
- **Every owner message and inbox card:**
  - the brief states each choice's outcome as the agent did, or is withheld: count `brief_withheld` events and
    read why;
  - options arrive whole (commas intact, none dropped);
  - context is whole or cut at a word with a mark;
  - "Why you" is correct;
  - no approval or routing claims appear inside the brief.
- **Rating:** rate each message 1 to 5 for "could a busy owner decide correctly from this alone" and say why.
- **Slack replies:**
  - `answer: X because Y` records;
  - a bare `ok` is not recorded as approval;
  - `sign off` signs;
  - `not me @X` hands on;
  - `rule if … until …` makes a rule;
  - a reply to an outdated message is handled;
  - a duplicate inbound event is applied once.

### 3. Resolve first: the reuse spectrum

Probe each row with a question written for it. Record the ladder outcome, the evidence line and whether the node is
authorized.

| Case | Expected |
|---|---|
| Exact repeat, same facts | resolved from memory as evidence, cites the decision, still wants a signature (unless a rule covers it) |
| Paraphrase, same scope | same as above |
| Same question, different customer or release | a prediction for another scope, names both values, not authorized |
| Required fact not stated | context for a person, not reused |
| Excluded fact present | not reused |
| Declared path does not match | not reused |
| Applicability expired | not reused |
| Rule, `auto_rules` off | still wants a signature |
| Rule, `auto_rules` on, facts match | covered by the rule, evidence names it, authorized |
| Rule, facts do not match | a person decides |
| Rule ended or expired | nodes it covered come back as needs-review; their tasks cannot finish |
| Earlier answer corrected | nodes that reused it are flagged for review; unsigned predictions withdrawn |
| Settled record answers it | resolved with a citation, wants a signature |
| Cancelled or superseded record | never the answer; shown as "left out, not adopted" |
| Open ticket proposes it | a prediction, not resolved |
| Answer composed across records, partly supported | stored as partly resolved |
| Memory and a newer record disagree on a figure | escalated, neither served |
| "Who decided X and why?" | answered from memory, naming the person and reason |
| Genuinely new | routed; no false reuse |
| Low-stakes naming or UX default | may be assumed and marked so; policy or security never assumed |
| Task-level `facts` | every node inherits them; a node's own value wins and shows |

- **False reuse** (anything authorized or resolved across a scope it should not cross) must be zero.
- **Missed reuse** (a person asked what was already settled for this scope) counts as annoyance.

### 4. The rest of the loop

- **Canvas:**
  - parent and child nodes;
  - `depends_on` holding dependents;
  - a child written before its parent was answered is flagged to re-read.
- **Follow-ups:**
  - a required follow-up holds the finish until it is adopted and answered;
  - an optional one can be left;
  - more than 10, or one over 2000 characters, is refused with a reason.
- **Waiting:**
  - the host uses `bridge_wait`;
  - confirm from host logs that waits are streamed with progress notices every 15 seconds and run to their
    timeout;
  - count wait calls per task (the last run needed 28 because each call was cut to 50 seconds);
  - an answer landing mid-wait returns the wait promptly;
  - node-scoped waits versus whole-task reads.
- **Finish:** refused while a node is pending or unsigned, a required approver is missing, a required follow-up is
  not adopted, or a person acted since the agent last read the tree. With a diff:
  - every signed decision is read;
  - reasons are whole or cut at a word with a mark;
  - each reading carries its requirement list, and every requirement read as met quotes a line that is in the
    diff;
  - plant one bypass the signed answer rules out (for example an early return before the new check) and confirm
    the reading comes back `unclear` with that path as its counterexample, not `follows`;
  - `unexamined` names what the reading depends on that the diff does not show;
  - write an independent test for at least one signed rule and compare it with the reading;
  - `verified` stays false and the caveat is kept;
  - time the reading. A finish without progress answers within about 45 seconds; when the reading is still running
    it says so with `review.status` running, and the result appears on `bridge_get_tree` under `review`. Confirm
    the host receives the reading one way or the other, and that a retry with the same diff does not read again;
  - `uncovered` names changed files whose decider was asked nothing on the task. On an engaged task the finish is
    refused for them: record whether the host then writes the decision for that person or gives a one-line reason
    per file, and whether the kickoff named that area as a candidate first. A typo task is never asked;
  - after a finish, correct and re-sign one answer its reading covered: the tree must show `review.status`
    `stale`, name the decision, and the next finish with the current diff must read it again.
- **Corrections:** correct a signed answer from Task A after Task B reused it, and follow the flags across tasks.
- **Permissions:**
  - an agent credential cannot answer or sign;
  - a viewer cannot sign;
  - a member without standing cannot sign;
  - an admin override is recorded as one.
- **Recovery:**
  - restart Raven while the host is waiting: nothing is lost, and the host resumes;
  - back up and restore, and compare table counts;
  - export.
- **UI:**
  - Needs you and Suggested;
  - the half-signed card;
  - Connections shows GitHub status;
  - mobile widths without overflow.

## Things that must never happen (each is a P0 or P1 if seen)

- An answer reused or authorized across a changed customer, release, path or expiry.
- A task finishing with a pending node, an unsigned answer, a missing required approver or an unadopted required
  follow-up.
- A question routed to the requester on inferred signals.
- A shown brief that ties an outcome to the wrong choice.
- An answer, reason or question cut mid-word without a mark.
- A bare `ok` recorded as approval.
- A cancelled, superseded or unratified record served as the answer.
- A hand-on granting authority the person did not choose.
- An agent credential answering or signing.

## Deliverables

1. `reviews/main-<rev>-core-e2e.md`. Lead with the verdict per core job (right person, right questions, resolve
   first, the loop). Then:
   - findings ranked P0 to P3, each with its evidence file and a reproduction;
   - what was real versus simulated;
   - what was not tested.

   Do not score an HTTP 200, a completed label or a process exit by itself.
2. `evals/newdev/results/main-<rev>-core-e2e/`:
   - `oracle.json` and `scoreboard.json`;
   - host transcripts (visible tool input and output only), owner messages, Slack inbound log, trees, finish
     responses, screenshots and probe scripts.

   The scoreboard fields:
   - routing accuracy;
   - decisions expected, found, missed and unnecessary;
   - touches per person against the minimum;
   - the reuse table, expected against actual;
   - false reuse and missed reuse counts;
   - briefs shown, withheld, and shown but wrong;
   - silent cuts;
   - host cost, turns, wait calls and idle aborts per run;
   - time from an owner's answer to the host resuming;
   - diff reading time.
3. Push both to `main`, and give a short summary: the verdict for each core job, the top findings, and what you
   could not test.
