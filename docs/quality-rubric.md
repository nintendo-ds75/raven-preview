# Quality rubric

What "better" means for Raven, front to back, scored 0–5 per dimension with the
evidence that justifies the score. A score moves only with evidence: a measured
number, a passing test that failed before, or a recorded run. The rubric is the
checklist for the improvement loop and for release readiness; it is not a
promise about any one score.

| Score | Meaning |
| --- | --- |
| 0 | Not working or never checked |
| 1 | Works in one hand-held case |
| 2 | Works, with known gaps a user will hit |
| 3 | Works for the documented path; gaps are known and listed |
| 4 | Measured against a target and meets it; regressions are caught by tests |
| 5 | Measured on real use with real people; no open P1 |

## Dimensions

### 1. Install and first run
A new person gets from `git clone` to a running workspace following only the README.
- **Measure:** minutes and steps from clone to "Create your workspace" done, on a clean machine; number of places the docs and the terminal disagree; errors seen.
- **Target:** ≤ 15 minutes, ≤ 3 commands, zero disagreements.
- **Checks:** `tests/test_setup.py`, `tests/web-setup.cjs`, a recorded fresh-install walkthrough with screenshots.

### 2. Connect a coding agent
Claude Code, Codex and Cursor each connect from one command, and the first ordinary prompt registers one task.
- **Measure:** per client: config written correctly, hooks load, one prompt → one task, follow-ups stay on it, a task registered twice is one task.
- **Target:** all three clients, documented and tested; the agent calls `bridge_start_task` without being reminded.
- **Checks:** `tests/test_host_sessions.py`, `tests/test_setup.py`, `tests/test_context_guidance.py`.

### 3. Task flow and invariants
The decision protocol holds under every path a real task takes.
- **Invariants:** nothing finishes while a decision is unsigned; an agent credential never answers, signs or invites; a corrected answer puts dependents up for review; a required follow-up can always be taken up, including after completion; a retry with the same key never duplicates; a restart loses nothing.
- **Measure:** scripted end-to-end flows against a real server; each invariant has a test that fails without its guard.
- **Target:** every invariant tested on SQLite and PostgreSQL; no open P1 from a customer-loop report.
- **Checks:** `tests/test_canvas.py`, `tests/test_finish_*.py`, `tests/test_postgres.py`, the open findings in `docs/customer-loop-2026-10-11.md`.

### 4. Routing: who gets asked
The first contact is the person who would actually decide, and a wrong person is rare.
- **Measure:** `python3 -m bench.routing.replay --datasets qemu --n 150 --seed 7 --conditions warm,pathless`: top-1, top-3, wrong-person rate; the deterministic historical replays.
- **Target:** warm top-1 ≥ 70 %, wrong-person ≤ 1 %, pathless top-3 ≥ 35 %; no regression item-by-item against the previous main.
- **Checks:** the bench, `tests/test_routing*.py`, `tests/test_signals*.py`.

### 5. What people read
Briefs, Slack messages, proposals, answers to task questions and resume reports lead with the point and contain nothing invented.
- **Measure:** length and first-sentence content of generated texts on a fixture set; brief check pass rate (`compose_brief` withholds a brief that fails its check); zero unsupported claims in a sample.
- **Target:** ≤ 3 sentences for a brief; first sentence states the decision or answer; 100 % of shown briefs pass the check.
- **Checks:** `tests/test_summaries.py`, `tests/test_brief.py`, `tests/test_delivery.py`.

### 6. What agents read
The instructions and tool descriptions an agent receives are enough to use Raven correctly and no longer than that.
- **Measure:** words injected per prompt by the hook; words of tool descriptions; tool-call sequence in a recorded run (does the agent call `bridge_start_task` first, `bridge_get_tree` before finishing).
- **Target:** ≤ 400 words per prompt, ≤ 2,500 words of tool text, no behaviour regression on recorded runs.
- **Checks:** `tests/test_bridge.py`, `tests/test_context_guidance.py`, recorded MCP transcripts under `evals/`.

### 7. UI correctness and accessibility
Every page works at desktop and phone width, by keyboard, with a screen reader, and says what to do next.
- **Measure:** axe-core violations per page; keyboard-only completion of answer, sign-off, hand-on; empty states with an action; no console errors.
- **Target:** zero serious or critical axe violations; every flow completable by keyboard; phone width without horizontal scroll.
- **Checks:** `tests/*-ui-unit.cjs`, `tests/browser*.cjs`, `tests/brief-*.cjs`.

### 8. Reliability
Raven survives the things that go wrong around it.
- **Measure:** restart during a task; Slack, GitHub, Jira and the model provider unavailable; a delivery that fails is retried and never lost; a report posted once.
- **Target:** every outbound message goes through one retryable outbox with an idempotency key; a restart resumes every waiting task.
- **Checks:** `tests/test_delivery.py`, `tests/test_host_wake.py`, `tests/test_sources_ops.py`.

### 9. Security and permissions
Only the right person can do each thing, and text from sources is never an instruction.
- **Measure:** probes: agent token on every write endpoint; viewer on every endpoint; CSRF and origin; brief-link scope; SSRF on every outbound URL; a source record containing instructions reaching a prompt.
- **Target:** every probe refused with a test; secrets never in logs or hook output.
- **Checks:** `tests/test_auth.py`, `tests/test_authz*.py`, `tests/test_security_isolation.py`, `tests/test_mcp_events.py`.

### 10. Test suite health
The suite catches regressions, runs fast enough to be run before every merge, and is green on every platform CI runs.
- **Measure:** wall time of the full suite; flaky runs per ten; modules under 70 % line coverage; failures that depend on the machine.
- **Target:** ≤ 7 minutes locally; zero flakes in ten runs; no module under 70 % that handles authority or money; green on macOS and Linux.
- **Checks:** CI, `python3 -m unittest discover -s tests -p 'test_*.py'`.

### 11. Documentation accuracy
The docs describe the product that ships, and a reader can act on them.
- **Measure:** disagreements between docs and code found by tests (tool list, endpoints, scopes, commands); a fresh reader completes getting-started without asking.
- **Target:** zero disagreements; every page reachable from `docs/README.md`.
- **Checks:** `tests/test_docs_links.py`, `tests/test_customer_text.py`.

### 12. Historical replay
On a repository frozen before a real change, with the real question set, Raven routes to the people who decided and resolves what the records already settled.
- **Measure:** the sealed cases under the evaluator's control: contacts right, questions resolved from records, no leaked future; a live coding-agent run scored against the rubric.
- **Target:** ≥ 80 % contacts right, no future leak, one scored live run per release.
- **Checks:** the evaluator's replay tools (`evals/audit`, `evals/chronological`).

## Scorecard

Kept outside the repository with each iteration's evidence; the current scores
are summarised in the improvement log. A dimension's score changes only in a
commit or report that names the evidence.
