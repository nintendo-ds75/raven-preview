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

### 13. Evidence intake and recording
A member can add evidence from the UI or Slack and the next investigation can read it; every record names who vouched for it; session records exist only when an admin turned them on and never carry what a person did not publish.
- **Measure:** seconds from paste to a cited record; records over HTTP with a caller-supplied scope (must be 0); refusals (oversize, viewer, kind `session` over HTTP, opted-out person); withdrawal effects.
- **Target:** pasted record cited by a scripted scan within one worker tick; 0 caller-supplied scopes; 20,001 chars refused with the meeting-import hint; `session_records` off on a fresh workspace; every withdrawal effect tested on SQLite and PostgreSQL.
- **Checks:** `tests/test_records_attestation.py`, `tests/test_session_records.py`, `tests/test_withdraw_record.py`, `tests/evidence-ui-unit.cjs`, `tests/test_slack_capture.py`, `tests/test_background_cost.py`.

### 14. Proposal quality
One bounded move with verbatim citations and four cases; counted good only by what people did with it.
- **Measure:** citation validity; case completeness; precision = signed / (signed + corrected + dismissed) from `GET /api/metrics` `work_proposals`; bench precision/recall on the labelled corpus.
- **Target:** 100 % of stored proposals have four cases and only verbatim quotes; invented quotes refused; bench precision ≥ 0.7 and recall ≥ 0.6; live precision reported, not targeted, until 20 real proposals exist.
- **Checks:** `tests/test_proposal_bench.py`, `tests/test_metrics.py`, `tests/test_objectives.py`.

### 15. Attention budget
Nothing reaches a person before a person asked for it; the one message a proposal produces reads like every other Raven decision; a default install costs nothing in the background.
- **Measure:** messages per proposal before and after a human click; words and JSON before the options line; open proposals per repository; subprocess spawns per poll on a default install; scans per corpus change.
- **Target:** 0 messages before "Review & take point"; exactly one deduped signoff message per proposal per person; ≤ 150 words and no JSON before the options; ≤ 1 open proposal per repository; 0 subprocess spawns across 20 polls; 1 scan per objective per corpus change.
- **Checks:** `tests/test_delivery.py`, `tests/test_background_cost.py`, `tests/test_objectives.py`, `tests/objective-decision-ui-unit.cjs`.

### 16. Learning
Dismissals are honoured, carry a reason and shape the next investigation; signed decisions resurface as evidence, never as approval.
- **Measure:** identical dismissed fingerprint re-shown; reasonless dismissals accepted; last five rationales present in the next model input and on Today; replay of signed decisions through the ladder.
- **Target:** 0 re-pitches with an unchanged fingerprint; 0 reasonless dismissals; rationales reach the model input and the aside in every scripted run; ≥ 5 of 6 replayed decisions resurface with signoff still required.
- **Checks:** `tests/test_objective_budget.py`, `tests/test_objectives.py`, `tests/test_decision_replay.py`, `tests/objectives-ui-unit.cjs`.

### 17. Positioning clarity
One sentence a VP of Engineering repeats, identical wherever it appears, with capability boundaries that match the code.
- **Measure:** README lead equals the positioning sentence; word count; every boundary link in `docs/positioning.md` resolves; every surface under `BRIDGE_MODEL_API=none` carries the fixed no-model notice; documented disagreements.
- **Target:** identical and under 35 words; 100 % of links resolve; 100 % of no-model surfaces carry the notice; zero disagreements.
- **Checks:** `tests/test_customer_text.py`, `tests/test_docs_links.py`, `tests/test_docs_reachable.py`, `tests/test_setup.py`, `tests/test_state_budget.py`, `tests/objectives-ui-unit.cjs`.

## Scorecard

Kept outside the repository with each iteration's evidence; the current scores
are summarised in the improvement log. A dimension's score changes only in a
commit or report that names the evidence.

Bench numbers on the synthetic corpus (`tests/fixtures/proposals`) prove
machinery; live precision is reported, not targeted, until 20 real
proposals exist.
