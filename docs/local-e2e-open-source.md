# Test Raven end to end on an open-source repository

This runs Raven the way a team would, on your own machine, against a real open-source codebase:
- a real coding agent (Claude Code) works on a real task;
- Raven decides who each judgment call belongs to;
- you answer as the owners in the web inbox;
- the agent finishes with what they decided.

Plan on half a day for the first run.

It has two phases:

1. **Who would Raven ask?** No setup beyond a clone. Raven reads the repository's history and ownership files and routes
   probe questions. You compare its picks with the people the project itself lists for each area. Real maintainers are
   named here, and that is the measurement: nobody is contacted and nobody answers as them.
2. **The full loop.** Raven runs in Docker with made-up stand-in owners mapped to the project's areas. A Claude Code
   session does real tasks through Raven, and you answer as the stand-ins.

## 1. Pick the repository

Measured on 30 September 2026 over the previous twelve months, from each project's own git history. Bot commits
are excluded. "Routed right" is phase 1 on a fresh clone, before GitHub sync and with no model.

| Repository | Language | Human commits | Authors | Authors with 10+ | Top author | Ownership file | Routed right |
|---|---|---|---|---|---|---|---|
| [prometheus/prometheus](https://github.com/prometheus/prometheus) | Go | 1,511 | 269 | 31 | 14% | CODEOWNERS: 26 people by area, plus MAINTAINERS.md | 7 of 8 routed; 4 of 12 honestly unrouted |
| [apache/airflow](https://github.com/apache/airflow) | Python | 7,887 | 914 | 111 | 10% | CODEOWNERS: 39 people over 117 paths | 12 of 12, to 9 different people |
| [home-assistant/core](https://github.com/home-assistant/core) | Python | 17,979 | 1,299 | 201 | 7% | CODEOWNERS: 761 people over 2,203 paths | 3 of 10 before GitHub sync |
| [django/django](https://github.com/django/django) | Python | 1,045 | 244 | 14 | 25% | none | 7 of 8 went to one Fellow |

- **Prometheus is the one to start with.** Owners are named by area, some areas are deliberately left to general
  maintainers, one listed owner is inactive, and the size is easy on a laptop (324 MB clone, 2 seconds to ingest).
  Its 4 unrouted areas (scrape, notifier, config, rules) are ones no ownership file covers and history has no clear
  owner. A coordinator is how Raven handles those, and Prometheus does the same with its general maintainers.
- **Airflow if you want Python.** The routing is excellent. Its dev environment is heavy, so running its tests
  takes more setup (`uv`).
- **Home Assistant is the hard test.** It has 761 named owners and one release manager who merges 80% of all
  changes. Its owners' GitHub handles rarely match their commit names, so Raven needs the GitHub token sync to tell
  that the listed owners are active. Run phase 1 with and without `--sync` and compare. That sync effect has not
  been measured yet.
- **Django is a control**, not a routing test: its Fellows commit every change, so history points at them.
- **Avoid single-maintainer projects** for this purpose: flask (one author has 88% of commits), attrs (89%),
  pydantic (53%), urllib3 (48%).

The full table for 30 repositories is in
`repo-stats.json` (internal evidence not included). Measure any other repository
with `python3 evals/newdev/oss_candidates.py owner/name --since YYYY-MM-DD`.

## 2. What you need

- macOS or Linux (Windows through WSL), with Docker Desktop running. Give it 4 CPUs and 8 GB.
- Git and Python 3.10 or newer on the host (phase 1 runs there). For Prometheus's tests, Go at the version its `go.mod`
  names.
- Claude Code installed and signed in (`claude --version`).
- An Anthropic API key for Raven's own model calls. This is separate from Claude Code's login.
- A GitHub fine-grained token with **public repositories, read-only** access.
- Access to the Raven repository.

**Rules for the whole run:**

- Never contact the project's maintainers, and never open issues or pull requests upstream or push to it. Work on a
  local branch.
- Raven names real maintainers when it routes. Do not answer as them. In phase 2 every answer comes from a made-up
  stand-in with a made-up name and an `.invalid` email, and no stand-in carries a real GitHub login.
- Keys live in `.env` and your shell only. Never paste one into a chat or commit it.
- Leave Slack off, or use a private test workspace you own. Without Slack, Raven sends nothing to anyone.

## 3. Get the code

```sh
git clone https://github.com/nintendo-ds75/raven-preview.git ~/bridge
git clone https://github.com/prometheus/prometheus.git ~/oss/prometheus
```

Use a full clone: history is the signal, so not `--depth`. Use a standalone clone, not a worktree.

## 4. Phase 1: who would Raven ask?

This takes about 15 minutes and needs no Docker.

```sh
cd ~/bridge
python3 evals/newdev/oss_routing_probe.py --checkout ~/oss/prometheus --repo prometheus/prometheus \
    --probes evals/newdev/oss_probes/prometheus.json --db /tmp/prometheus-probe.db
```

Then the same questions with GitHub review data. Use a fresh database; this reads up to 300 merged pull requests,
about a thousand API calls against the token's 5,000 an hour:

```sh
export GITHUB_TOKEN=...   # the read-only token, in your shell only
python3 evals/newdev/oss_routing_probe.py --checkout ~/oss/prometheus --repo prometheus/prometheus \
    --probes evals/newdev/oss_probes/prometheus.json --db /tmp/prometheus-probe-synced.db --sync
```

Each line gives the person Raven chose, whether the project's own CODEOWNERS or MAINTAINERS lists them for that
area, and the evidence. For every "inferred" line, check by hand: is that person the one the project's
`MAINTAINERS.md` or CODEOWNERS names, perhaps under their GitHub handle? Then add 10 to 20 questions of your own
across areas, including areas no ownership file covers, to a copy of the probe file.

Keep this output on your machine: it names real people.

## 5. Phase 2: set up Raven for the full loop

### Configure and start

```sh
cd ~/bridge
cp .env.example .env
```

Put these two lines in `.env`. The GitHub App flow in the web setup only works on repositories you own, and a token
reads any public one.

```dotenv
GITHUB_TOKEN=github_pat_...
BRIDGE_GITHUB_REPOS=prometheus/prometheus
```

Then:

```sh
./setup --repo ~/oss/prometheus --repo-name prometheus/prometheus
```

Answer the prompts:

| Prompt | Answer |
|---|---|
| Connect agent MCP clients | `claude`. This writes `~/oss/prometheus/.mcp.json` with a limited agent token and keeps it out of Git. |
| Configure Anthropic model-assisted retrieval | `y`, then paste the key (hidden) |
| Slack, Teams, managed execution | `N` |

The browser opens <http://localhost:7333>.

### Create the workspace

1. Run `./dev login` for the one-time setup credential. Use it on **Create your workspace**, then create your
   profile. You become admin.
2. Open **Connections & setup**. GitHub reads "Token set", then "On" with `prometheus/prometheus` after the first sync.
   The first sync starts with Raven and repeats every 15 minutes, and `./dev logs` shows it.

### Stand-in owners (People & ownership)

Mirror the project's areas, never its people. For Prometheus:

| Stand-in (made up) | Email | Add under "Record who decides" |
|---|---|---|
| Tess Tsdb | tess@example.invalid | decides, path, `tsdb/*` |
| Remy Remote | remy@example.invalid | decides, path, `storage/remote/*` |
| Quinn Query | quinn@example.invalid | decides, path, `promql/*` |
| Rory Release | rory@example.invalid | three rows: decides, path, `docs/*`; decides, path, `CHANGELOG.md`; **must approve**, path, `config/*` |
| Sasha Secure | sasha@example.invalid | decides, category, `security` |
| Gale General | gale@example.invalid | none: make them the **Coordinator** under Routing settings, at the top of the page |

- Leave `scrape/`, `notifier/` and `rules/` unmapped on purpose. Questions there should reach Gale, as Prometheus
  leaves them to its general maintainers.
- Leave **Pilot mode** off, so history still counts where your map is silent.

To answer as a stand-in, invite them: **Invite teammate**, member, copy the link. Open each link in its own browser
profile or private window and set a password. No email is sent.

### Connect Claude Code

Setup already wrote `.mcp.json`. Tell the agent to use it: create `~/oss/prometheus/CLAUDE.md` with the text below,
then keep it out of commits with `echo CLAUDE.md >> ~/oss/prometheus/.git/info/exclude`.

```markdown
## Raven

This team uses Raven (the `bridge` MCP server) to route the judgment calls in a task to the person who owns them.
At the start of every task, before reading or editing anything, call `bridge_start_task`, then follow what Raven
returns for the rest of the task.
```

Start `claude` in `~/oss/prometheus`, approve the project's `bridge` server when asked, and check `/mcp` shows it
connected. If it does not, **Connections & setup → Connect an agent → Claude Code** gives a
`claude mcp add --transport http bridge …` command to run instead.

## 6. Run the tasks

Work on a local branch: `git -C ~/oss/prometheus switch -c bridge-e2e`. Run each task as its own Claude Code
session, typed in as the task, with no list of decisions or owners.

- **Interactive:** run `claude`, paste the task, and watch `/cost`.
- **Scripted, with a budget:**

```sh
cd ~/oss/prometheus
claude -p "$(cat ~/tasks/task1.txt)" --mcp-config .mcp.json --permission-mode acceptEdits \
  --allowedTools "mcp__bridge" "Bash(go test:*)" "Bash(go build:*)" "Bash(git diff:*)" "Bash(git status:*)" \
  --max-budget-usd 10 --output-format stream-json --verbose > ~/tasks/task1-host.jsonl
```

The tasks below are checked against Prometheus as of this writing: none of this behavior exists yet, and each one
forces decisions that belong to different owners.

1. **Seed.** "Retry alert batches that an Alertmanager rejects with 429 or 5xx, honoring its Retry-After header.
   Opt-in and off by default, so nothing changes for anyone who does not turn it on. This is for release=next."
   Expect:
   - questions for Gale (notifier, and config, which no file maps);
   - Rory's required sign-off on the config change;
   - Rory on docs and the changelog.
2. **Cross-area.** "Add an opt-in per-job setting that keeps a target's last scraped samples for up to N failed
   scrapes before they go stale. This is for release=next." Expect:
   - Gale for scrape;
   - Quinn if staleness semantics come up;
   - Rory for config and docs.
3. **Reuse.** Start it after task 1 is answered. "Give HTTP service discovery the same opt-in 429 and 5xx retry, with
   Retry-After, as the Alertmanager notifier. This is for release=next." Expect:
   - the policy answers from task 1 come back as evidence to sign, not as new questions;
   - only what is new about discovery is asked.
4. **Scope change.** Repeat one question from task 1 for `release=v2.55-backport`. Expect a prediction for another
   scope that names both releases and is not authorized.
5. **Trivial.** "Fix a typo in the documentation." Expect `pass` and no questions to anyone.

## 7. Answer as the owners

In each stand-in's window, go to **Needs you**, open **Review**, and read the brief and the agent's own words. Then:

- answer with a reason;
- sign off, or correct an answer;
- hand a question on ("this question only", or teach Raven who decides);
- add follow-up questions, marking one **Required**;
- make one answer a reusable rule, with a condition and an expiry, and turn on **Automatic rules** (Routing settings) before task 3.

After each message, write down whether a busy owner could decide correctly from it alone, and anything Raven
should have known without asking.

## 8. Finish and check

The agent finishes with `bridge_finish_task`, passing its diff and what it ran. The finish is refused while anything
still waits on a person, and, on an engaged task, while the diff changes a file someone else decides that nothing
on the task was put to: note whether the agent then writes that decision for them or explains why the change
settles nothing (`uncovered`). Then:

- run the tests yourself, for example `go test ./notifier/... ./config/... ./scrape/...`;
- read the finish report: a model reads each signed decision against the diff (follows, departs or unclear), with
  the line of the diff behind each requirement, any counterexample it found and what it could not examine, and
  `verified` stays false. Check every counterexample yourself, and write an independent test for at least one
  signed rule: the reading is not a test;
- check **Tasks** for the task tree, and **Decision memory** for the answers.

## 9. What to record

| Topic | What to write down |
|---|---|
| Right person | For every question, who Raven chose against who should decide. In phase 1, that is the project's lists; in phase 2, your map and coordinator. |
| Right questions | How many were asked, how many were needed, any asked twice, and any asked that records or earlier answers already settled. |
| Reuse | In tasks 3 and 4: what came back as evidence, what as a prediction, and what as a new question. Unsafe reuse, meaning anything authorized across a changed release or scope, must be zero. |
| Owner messages | Was the brief faithful? Were the options whole? Were any briefs withheld (a `brief_withheld` event says why)? |
| The agent | Cost, turns, number of `bridge_wait` calls, and any "no response or progress" aborts. |
| Finish | Each refusal and why; the diff reading time; whether reasons were whole. |

`evals/newdev/core-e2e-prompt.md` has the full checklist and scoreboard if you want the complete version.

## 10. When something is off

| Problem | What to do |
|---|---|
| Something looks wrong | `./dev status` and `./dev logs` |
| GitHub is not On | Check both `.env` lines, then `./dev up`. |
| The agent never calls Raven | Check the `CLAUDE.md` rule and `/mcp`. |
| Port 7333 is taken | Use `./setup --port 7444`. |
| You pulled new commits into the clone | `./dev up` re-ingests when HEAD moves. |
| You want to stop | `./dev down` keeps all data. `docker compose down -v` deletes it; only use it to start over. |

## Hand it to an agent

[`evals/newdev/local-oss-e2e-prompt.md`](../evals/newdev/local-oss-e2e-prompt.md) is a prompt that has a coding agent
on your machine run all of this:
- phase 1 on its own;
- the setup;
- each task as a separate Claude Code session;
- the write-up.

It stops and tells you whenever a stand-in has something to answer, so the answers stay yours.
