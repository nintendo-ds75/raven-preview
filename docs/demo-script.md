# Demo script: install Raven and run one task through the inbox

One take, under eight minutes, no model key on the Raven side. Two people
appear on screen: **Sam**, who installs Raven and runs Claude Code, and
**Dana**, the product owner whose decision the task needs. Use a second
browser profile or a private window for Dana. Everything the viewer sees
comes from the deterministic path: CODEOWNERS, git history and the signed
answer. A model key only adds written briefs, proposals and the diff review.

Before recording: Docker Desktop running; Claude Code signed in; a sample
repository with a `CODEOWNERS` file and a few commits by the people in it
(the one used for the rehearsal is described at the end); nothing on port
7333.

| # | Do | The viewer sees | Say |
| --- | --- | --- | --- |
| 1 | In a terminal: `git clone https://github.com/nintendo-ds75/raven-preview.git && cd raven-preview && ./setup --repo ~/code/fetchkit --repo-name acme/fetchkit` | The build runs, then `Raven is running. Open: http://localhost:7333` and the setup credential `brg_…` above it. | "One command builds Raven, starts PostgreSQL and indexes the repository's history, so Raven already knows who owns what." |
| 2 | Open `http://localhost:7333`. | **Create your workspace**, step 1 of 2: workspace name and setup credential. | "The credential proves I operate this install; nobody else can claim the workspace." |
| 3 | Type `Acme`, paste the credential, **Create workspace & continue**. | **Create your profile**, step 2 of 2. | "The first profile is the admin." |
| 4 | Name, email, a 12-character password, **Create admin profile**. | **Connections & setup** opens: three numbered cards. | "Three steps: connect an agent, give Raven context, invite the people who decide." |
| 5 | Step 1: click **Connect** on the Claude Code card. | A dialog with one `claude mcp add …` command and, below it, the Raven rule for `CLAUDE.md`. | "Raven mints a limited credential: it can register tasks and write decisions, it can never answer or sign." |
| 6 | **Copy setup**; in the sample repo's terminal paste the command. Then **Copy instruction** and paste it into `CLAUDE.md`. | The terminal confirms the MCP server; `CLAUDE.md` ends with `## Raven …call bridge_start_task`. | "The rule is what makes the agent start every task in Raven instead of just holding the tools." |
| 7 | Step 2: scroll to **Give Raven context**. | The card; the repository was indexed in step 1. | "History and CODEOWNERS are already in. GitHub, Slack channels and Jira are optional evidence." |
| 8 | Step 3: **Invite teammate** → `dana@example.test`, Member, **Create invitation**. | A private invitation link. | "Dana owns the retry policy. She needs an account to sign." |
| 9 | Open the link in Dana's window; name, password, **Accept invitation**. | Dana's inbox, **Assigned to me**, empty. | "Nothing waits on her yet." |
| 9b | Sam's window: **Workspace tools → People & ownership → Add authority**: Dana Okafor, *decides*, path `fetchkit/retry.py`, repository `acme/fetchkit`, note "Retry policy is a product call". **Record authority**. | The authority map gains one row. | "CODEOWNERS already points at Dana for this file, but Lior wrote most of the code. Recording who decides once makes it her call whatever git says." |
| 10 | In the sample repo's terminal: `claude` then type `Make the retry limit configurable; the default is a product decision.` | The hook registers the task; Claude reads the tree, edits `retry.py`, writes one decision node and says it is waiting for Dana. | "Claude does the engineering. The default value is not its call, so it asks." |
| 11 | Switch to Dana's inbox. | Within five seconds a card: the question, the agent's context (file, revision, the July commit), **Dana Okafor** as owner. | "Raven routed it to Dana and says why: she decides `retry.py`, CODEOWNERS agrees, and she authored the change that set the limit." |
| 12 | Click **Review**. | The decision dialog: context, the evidence the agent cited, the routing reason, and either the answer form or, when the agent proposed an answer itself, that proposal with **Sign off as Dana Okafor**. | "Evidence first, then the decision. A proposal from the agent is not approval until she signs." |
| 13 | Either answer (`Keep the default at 3 retries.` / `Three keeps checkout p99 inside its latency budget.`) and **Record decision**, or read the proposal and **Sign off as Dana Okafor**. | The card turns **Signed · Dana Okafor**; the inbox badge drops to 0. | "Her signature is recorded against the exact text she signed." |
| 14 | Back to the terminal. | `bridge_wait` returns the answer; Claude finishes, calls `bridge_finish_task`, reports done. | "The agent was woken by the signature, not by me. It could not finish before it." |
| 15 | Sam's window: **Tasks** → **View run**. | The run: verdict `engage` and why, the decision with Dana's answer, who signed, the diff and checks. | "The whole run is on one page: what was asked, who decided, what shipped." |
| 16 | (If time) **Open task page**. | The task page a Slack notification links to: the decision, the evidence, the answer form. | "This is what Dana would get in Slack instead of the inbox." |

## Timing

Measured on the rehearsal runs; see the timing table in the run report.
Steps 1–9 are setup (about 3 minutes with a cached build; the first build adds
5–10 minutes before recording). Steps 10–15 are the task (about 3 minutes, of
which the agent spends most of its time waiting on Dana).

## What to say if asked

- *Where is the model key?* Raven runs without one. A key adds model-written briefs, answer proposals and the advisory diff review; routing, the finish gate and signatures are deterministic.
- *Why did it name Dana?* The card states it: the authority recorded in step 9b, CODEOWNERS for `retry.py`, and her commit that set the limit. Without step 9b the inferred signals alone can go to Lior when the agent names `client.py` as well, because he wrote most of both files; that is why the script records who decides before the task.
- *Can Sam just answer?* No. Sam is an admin; the form is read-only for him unless he records an explicit override.

## The sample repository

`acme/fetchkit`: a small Python client with `fetchkit/retry.py` (`MAX_RETRIES = 3`),
`.github/CODEOWNERS` naming `@lior-bensimon` for `/fetchkit/` and `@dana-okafor`
for `/fetchkit/retry.py`, and five commits by those two people, one of them Dana's
"keep the retry limit at three" with the product reasoning in the body. The
rehearsal built it with `scripts/`-free shell; any repository with CODEOWNERS and
real authors works the same way.
