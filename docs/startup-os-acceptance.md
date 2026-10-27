# Startup OS acceptance ledger

This is a coverage ledger, **not a claim that all integrations work live**.
`Contract` means an offline behavioral test with explicit model/provider doubles;
`runtime` means the installed native SDK/server with scripted inference;
`CI pending` is never a pass.

Every commit hash below is a **historical checkpoint**: what CI or a local run
observed at that commit, on that day. None of it transfers to later commits.
The verification of the final head of any change lives in that change's pull
request and its CI runs, not in this document; read the job results there, and
treat the `docker-verification` and `codex-native` jobs, which run as advisory
because they depend on external image pulls and an alpha npm pin, by their own
result rather than by the overall check.

Historical checkpoints recorded here: at remote `713ecaa` (local tree
`81acac1`) CI passed real Chromium UI/API interactions, installed Pi/OpenCode
scripted loops, and real Docker independent verification, while the PostgreSQL
and full Python lanes were still running when this ledger was written. A local
aggregate at that tree collected 3,025 tests: 3,018 passed and seven skipped
(four Docker tests and three native Codex turn cases that run separately in CI).
At remote `503f961`, all eight native Codex tests passed without skips, including
fresh/resumed adversarial refusals, no native execution lifecycle, exact tool
schema, positive context continuity and interruption/resume. The later
experimental enablement is restricted to that exact pin/model and must pass
the same suite through the public worker entrypoint on whatever head ships it.

| Design case | Current evidence / remaining qualification |
|---|---|
| 1 Outcome → bounded preparation | Implemented explicit preparation checkbox and native Workbench dispatch; HTTP/module contracts. No configured authenticated live host here. |
| 2 Transcript → cited proposal, no premature work | Head/research/module contracts; model actions scripted. |
| 3 Core review → exact signed activation once/restart | `test_objectives`; persistent request/run identities and ordinary core signoff. |
| 4 Negative/changed/unauthorized/stale approval | Objective and existing core authorization contracts; no new approval engine. |
| 5 Duplicate events/submissions/joins | Objective idempotency and meeting durable-intent contracts; Vexa mutations are not advertised exactly-once. |
| 6 Completed task ≠ achieved outcome | Separate objective lifecycle and explicit human attestation; contract coverage. |
| 7 New source → same thread | Full-corpus research identity, retained source links and factual-progress wake of saved waiting worker; real Workbench same-task factual continuation regression passes (inference scripted). |
| 8 Rejection/no-op | Dismissal, stale dismissal, cache/freshness and factual/no-op contracts. Semantic relevance depends on configured model. |
| 9 Pause/stop races | Objective controls, lease-owner fencing and core/browser completion fences; contracts. |
| 10 Investigate retrievable facts | Actual bounded search/read/list loop; 270k transcript early-chunk retrieval contract. Repository source facts only: this head does not execute arbitrary code research. |
| 11 Missing permission remains blocking | Existing core signoff and unsigned proposal tests; model cannot sign. |
| 12 Same-action ambiguity avoids interruption | Research factual/no-material dispositions supported; no universal semantic classifier guarantee. |
| 13 Cases inside signed scope | Positive/negative/boundary/non-implication cases retained, rendered and sent completely to worker; bound scope/hash contracts. |
| 14 One boundary across outputs / no exception leakage | Exact objective + repository + signed move binding; no general CRM/marketing executor added. Existing authority scope remains authoritative. |
| 15 Amendment stales outputs | Objective worker and verification bindings checked against current signed scope; contracts. Historical evidence retained. |
| 16 Amendment at finish | Final DB writer fence in core run completion and browser result publication; regression. |
| 17 Host claims pass, independent check fails | Real-git/fake-sandbox verifier contracts; actual Docker nonzero-exit lane passed at checkpoint `713ecaa`. |
| 18 Changed bytes stale receipt | Candidate tree/patch/base/profile/core bindings; contracts. |
| 19 Check mutates candidate | Post-command input mutation detection; actual Docker mutation checks passed at checkpoint `713ecaa`. |
| 20 Agent edits tests | Operator-held profile supports separate tests; actual Docker signed-case positive/negative fixture passed at checkpoint `713ecaa`. Not an adversarial oracle: mutate-and-restore inside the same sandbox is not prevented. No automatic semantic test synthesis claimed. |
| 21 Sandbox/profile unavailable | Explicit unavailable result; contract. Docker absent locally. |
| 22 Revoked source copied downstream | Objective/proposal/cache/worker/verification disclosure regressions. |
| 23 Import ≠ attendance | Distinct import/Vexa capabilities and UI labels; contracts. |
| 24 No consent → no join | Per-occurrence consent and provider dispatch contracts. |
| 25 Transcribe without media retention | Separate explicit flags; contract request bytes checked. |
| 26 Unknown join outcome | Durable intent and nonretryable unknown state; contract. |
| 27 Provider lifecycle distinctions | Vexa status/stop/reconciliation and consented durable read-only observer fixtures; failed/empty/partial completion explicit. No actual bot run. |
| 28 Transcript injection ≠ authority | Source content never drives dispatch without core review; provider destinations server-configured. Model instruction-following is not a substitute for gates. |
| 29 Disclosure permission separate | Separate workspace-shared import/background polling attestations; current administrator/consent checks before and after reads; source-wide trust-boundary checks. |
| 30 Stop/revoke/delete/backups distinct | Operational projection preserves capture controls after local withdrawal; lifecycle tests. Backup erasure not claimed. |
| 31 Exact native session resume | Claude protocol contracts; Codex actual pinned initialize/schema, native tool/context resume and interruption/resume passed at `503f961`; experimental dispatch limited to the tested pin/model; Pi/OpenCode installed-runtime scripted loops. Authenticated live loops unverified. |
| 32 Version/config fail closed | Native capability and version checks; no silent runtime/provider fallback. |
| 33 Native tool containment | Pi explicit tools/resource loader; OpenCode guarded native runtime; Claude tools-only configuration; Codex valid native shell/file/image/network/goal/question refusals plus absent forbidden execution lifecycle passed on fresh/resumed turns at `503f961`. Exact pin/model only, no universal containment claim. Public-entrypoint final-head CI remains required. See harness docs. |
| 34 Stable tool IDs | Durable parent intent dedup/conflict contracts; OpenCode replay protocol tests. |
| 35 Crash during write | Existing unresolved operation ledger; no blind replay of unknown writes. |
| 36 Accepted prompt vs terminal result | Explicit native delivery receipt and terminal-result contracts. |
| 37 Abort before/after delivery | Pending input preserved without delivery; native cancellation and supervisor tool-drain regressions. |
| 38 Secrets/errors | Credential allowlist and safe failure handling tests; no credential values written into this report. |
| 39 XSS | Node DOM/API-double checks; Chromium passed at checkpoint `713ecaa`. |
| 40 Interrupted/repeated UI flows | Node/real HTTP checks; native Chromium desktop/mobile/cancel/refresh and readable exact-source evidence passed at checkpoint `713ecaa`. |
| 41 Existing flows and migrations | Initial 2,861-test baseline passed (4 skipped); intermediate integrated 3,001-test suite passed (9 skipped). Both are historical local runs; final-head checks and PostgreSQL CI results belong in the PR. |
| 42 Exact reporting | Separate source tests, installed runtimes, real containers, browser and live provider stages. Local Docker/browser/PG unavailable; approved CI closes only stages it actually executes. |

## Setup limits observed in the implementation workspace

Safe capability inspection found all dedicated `RAVEN_HOST_*_API_KEY` and model
settings absent. Pi 1.4.0 is installed; the bundled official Codex experimental pin initializes and matches generated schemas, while the unrelated system Codex 0.159.2 does not match it. Full native turn tests passed in CI with scripted local inference; experimental dispatch requires that exact binary/model. This does not establish authenticated provider acceptance. No authenticated live execution was attempted.
Meeting attendance was neither requested for a real call nor tested. Optional
runtime tests use synthetic repositories and scripted local providers.
