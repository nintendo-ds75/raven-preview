# Bridge scaling roadmap

Living backlog for scaling decision retrieval, agent traffic, and team usage. Update this document as we discover constraints, make architecture decisions, and complete work.

Last updated: 2026-09-13

## Current baseline

Bridge is a local, single-operator prototype. Million-decision capacity has **not** been benchmarked or demonstrated.

- Suggestion search scans all approved decisions, filters by owner in Python, calculates keyword overlap, and sorts matches before returning five candidates. The top candidate supplies the suggested answer. See [`Store.candidates`](bridge/store.py).
- Matching uses question text, not the new task's context or the prior answer's rationale. Word overlap is not a confidence score.
- Request creation performs retrieval inside a `BEGIN IMMEDIATE` write transaction. Slow retrieval can delay other writers.
- `/api/state` returns all decisions, runs, and owners. The browser refreshes it every five seconds. See [`server.py`](bridge/server.py) and [`app.js`](web/app.js).
- Corrections preserve old answers and withdraw pending suggestions derived from the corrected decision. Stale inbox submissions are rejected using the decision's update timestamp.
- Owners are manually configured. Repository names are recorded, but routing and retrieval do not enforce repository or organization boundaries.

## How to maintain this backlog

Use stable IDs so issues, changes, and benchmark reports can reference these items. Status values: **Planned**, **In progress**, **Blocked**, **Done**. All items below are planned and unassigned; their order is a proposed sequence, not a delivery commitment.

For each item started, record its owner, implementation link, and any dependencies. Mark it done only after recording validation evidence. Add newly discovered bottlenecks here and update the change log. Record architecture choices separately from options still being evaluated.

## First: measure and remove work that grows with the entire database

### SCALE-01 · Establish a reproducible performance baseline

Status: Measured · `python3 -m bench.scale.measure` seeds N signed decisions and times one question, whole-scan and bounded; the numbers below are from `bench/scale/results/` on the reference container (median of 15 questions, FTS5 on).

| memory rows | whole scan: nearest answers | whole scan: memory search | bounded: nearest answers | bounded: memory search | top-1 agrees with the whole scan |
| --- | --- | --- | --- | --- | --- |
| 2,000 | 89 ms | 223 ms | 45 ms | 73 ms | 0.67 |
| 8,000 | 383 ms | 3,496 ms | 86 ms | 131 ms | 0.87 |
| 20,000 | 963 ms | 32,577 ms | 142 ms | 194 ms | 0.87 |

These are 15 synthetic questions per size from `bench/scale/results/20260920-012742.json`, and "agrees" is agreement with the whole scan's top-1, not accuracy against a human label: the whole scan is not ground truth either. At 2,000 rows Bridge scans whole in production (`FULL_SCAN_MAX`); the bounded figure there is the bound forced on for measurement. What the pilot needs measured, on its own questions, is whether the right evidence is in the candidate set, how often the wrong thing is reused, how often Bridge abstains, and what that costs in human interruptions; the bound's coverage (`CANDIDATE_FTS`, `CANDIDATE_RECENT`) is raised, or a measured fallback added, before any learned embedding is considered.

- Build isolated benchmark datasets at 10,000, 100,000, and 1,000,000 decisions, with realistic text lengths, owner distributions, and approval states.
- Exercise common and rare queries, no-match queries, heavily represented owners, concurrent requests, and concurrent approvals.
- Measure p50/p95/p99 latency, throughput, peak memory, response sizes, database/index size, and lock waits or errors. Separate cold and warm runs.
- Record hardware, workload, dataset seed, and code revision. Agree on latency, concurrency, and resource targets before declaring readiness.

Done when: a repeatable command produces a baseline report, agreed targets are recorded, and later implementations can be compared against the same workload.

### SCALE-02 · Replace full scans with indexed candidate retrieval

Status: Done for memory · A memory of at most `FULL_SCAN_MAX` (2,000) rows is scanned whole, exactly as before; above it every similarity pass scores a bounded candidate set: the FTS5 BM25 matches of the question's terms (`CANDIDATE_FTS`, 600) plus the newest rows (`CANDIDATE_RECENT`, 500). The ladder passes the question's text to every memory lookup so the bound uses its words. The cost of a question stops growing with the memory (194 ms at 20,000 rows against 32 s); the bound disagrees with the whole scan's top-1 on one synthetic question in eight at 8,000 and 20,000 rows and one in three when forced on at 2,000, so the bound's coverage is the first thing to widen when a pilot's own questions show misses, and a learned embedding (P3.3 in the plan) comes only after that is measured.

- Apply owner and approved-status filters in the database and add indexes suited to the actual query patterns.
- Evaluate SQLite FTS5 with relevance ranking as the first full-text retrieval option. Retrieve a bounded candidate set before doing application-level scoring.
- Include question, context, and rationale in the retrieval design; evaluate field weighting rather than assuming all text is equally useful.
- Support backfilling and rebuilding the search index, and keep it synchronized with answers and corrections.

Done when: query plans and benchmarks demonstrate indexed retrieval with bounded candidate processing; correctness tests cover empty results, owner filtering, and index updates after corrections.

### SCALE-03 · Keep expensive retrieval outside write transactions

Status: Planned · Owner: Unassigned

- Move search and any future model calls outside the transaction that creates a request.
- Before saving a suggestion, revalidate the selected source's approval state, revision, and ownership so a concurrent correction cannot introduce stale advice.
- Keep transactions short and define behavior when a candidate changes during retrieval: retry or save the request without a suggestion.

Done when: concurrent request and approval tests show reduced write contention, and a correction during retrieval cannot produce a suggestion based on an obsolete answer.

### SCALE-04 · Bound dashboard queries, updates, and exports

Status: Partly done · `GET /api/inbox`, `GET /api/decisions` and `GET /api/runs` page on the server with database-side filters (status, owner, task, a word); `/api/state` still carries the newest 500 runs and decisions plus every blocking decision for the current inbox, which is the next thing to move onto the paged endpoints.

- Replace whole-workspace downloads with paginated endpoints for inbox, runs, memory, owners, and activity.
- Perform filtering and search on the server; compute summary counts without fetching every row into the browser.
- Use stable cursors and incremental updates. Evaluate event notifications only if incremental polling is insufficient.
- Stream or process large exports in a background job rather than constructing the entire export in application/browser memory.

Done when: ordinary page loads and refreshes return bounded results at one million decisions; pagination handles concurrent additions without duplicate or missing records under its documented consistency model; exports preserve complete revision history.

## Next: improve suggestion quality without weakening review

### SCALE-05 · Build a retrieval-quality evaluation set

Status: Planned · Owner: Unassigned

- Collect reviewed examples of equivalent questions phrased differently, similar questions with incompatible constraints, exceptions, conflicting decisions, and cases with no applicable precedent.
- Label which prior decisions are relevant and whether their answers apply to the new context.
- Measure candidate recall, relevance of top results, incorrect suggestions, and appropriate abstention. Keep a held-out evaluation set.

Done when: a repeatable evaluation reports both useful matches and misleading suggestions, with acceptance thresholds agreed before comparing retrieval approaches.

### SCALE-06 · Evaluate semantic retrieval and context-aware ranking

Status: Planned · Owner: Unassigned · Depends on: SCALE-02, SCALE-05

- Compare the indexed keyword baseline against semantic retrieval and a combination of both.
- Re-rank only a bounded candidate set using task constraints, prior rationale, scope, and applicable exceptions.
- Return no suggestion when evidence is insufficient or conflicting. Keep sources inspectable and distinguish retrieval scores from calibrated confidence.
- Measure latency and operating cost alongside quality. Select models, providers, or vector infrastructure only after evaluation.

Done when: the selected approach improves the agreed quality metrics within the performance/cost budget, and suggestions always remain pending until reviewed.

### SCALE-07 · Preserve provenance and freshness at scale

Status: Planned · Owner: Unassigned

- Introduce explicit decision revisions and source-revision references for suggestions.
- Index dependency lookups so corrections do not scan unrelated decisions; propagate invalidation to search indexes and any future caches.
- Define policy expiry, supersession, and conflict handling. A historical approval must not silently become authorization for a new task.
- Preserve human-readable explanations and audit history through migrations, rebuilds, and exports.

Done when: tests cover correction chains, concurrent review, stale caches/indexes, expired decisions, and conflicting sources without losing provenance or presenting obsolete advice as approved.

## Before shared or hosted team use

### SCALE-08 · Enforce workspace scope and authenticated ownership

Status: Planned · Owner: Unassigned · Gate: required before shared/hosted rollout

- Add organization, workspace, and repository scope to decisions, ownership rules, runs, and search records.
- Authenticate humans and agents. Authorize reads, requests, reassignment, approvals, and exports separately.
- Apply access boundaries during retrieval and before returning results; carry them into indexes, caches, and background jobs.
- Define fallback routing, ownership changes, and delegation. Evaluate CODEOWNERS/git ingestion with traceable routing explanations.

Done when: isolation tests prevent cross-workspace disclosure, unauthorized actors cannot approve decisions, and identical paths in different repositories route correctly.

### SCALE-09 · Make delivery and retries reliable

Status: Planned · Owner: Unassigned · Gate: required before automated external delivery

- Add idempotency keys so retried agent requests do not create duplicate tasks or questions.
- Use durable delivery records with retries, backoff, deduplication, and failure visibility for future notifications and connectors.
- Define bounded polling, timeouts, cancellation, and recovery after disconnects or restarts.
- For Slack/Teams, verify responder identity and correlate replies to the correct request and revision.

Done when: restart and failure tests show no lost requests, duplicate approvals, or misattributed replies; operators can inspect and retry failed delivery.

### SCALE-10 · Select storage and operations based on measured load

Status: Planned · Owner: Unassigned · Depends on: SCALE-01–04; SCALE-08 before shared rollout

- Reassess SQLite after indexed retrieval and bounded queries are benchmarked. Evaluate a server database when write concurrency, deployment topology, or availability requirements justify it.
- Add instrumentation for retrieval latency, payload sizes, lock contention, pending-request age, failed delivery, and suggestion corrections.
- Establish tested backups, restoration, schema/index migrations, retention, and archival procedures.
- Load-test the complete workload, including approvals, reads, search-index maintenance, and exports—not search alone.

Done when: capacity limits and operational targets are documented with evidence, recovery is exercised, and any storage migration has a tested rollout and rollback procedure.

## Open decisions

- Expected total decisions, active owners, concurrent agents, and organizations?
- Required interactive latency, throughput, availability, and retention?
- Local installation, shared private deployment, or hosted service as the next deployment target?
- What rate of misleading suggestions is acceptable, and when should Bridge abstain?
- What task context may be sent to an external model provider, if semantic/model-based ranking is adopted?

These questions are inputs to implementation and benchmarks; they do not block maintaining this backlog.

## Architecture decisions and evidence

- 2026-09-13: Retain the current prototype while planning improvements. No database migration, search-engine choice, or model/provider choice has been made. No million-decision benchmark exists yet.
- Reference option for SCALE-02: [SQLite FTS5 documentation](https://www.sqlite.org/fts5.html).

## Change log

| Date | Update |
| --- | --- |
| 2026-09-13 | Created the backlog from the suggestion-search review: full scans, write contention, whole-workspace polling, retrieval quality, and requirements for reliable shared usage. |
