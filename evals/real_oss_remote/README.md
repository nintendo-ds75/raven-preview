Latest: lightweight review of main 24b34c9 (not included in this preview). Detailed earlier evidence: full component retest of 0d7c013 (not included in this preview). The report below is historical.

# One full real-host workflow on main `1c56c79`

**The decision handoff loop works. The advertised autonomous workflow still needs an assisted pilot.** A real Codex host investigated a real Grafana bug, wrote two connected decisions, survived a process interruption, received simulated owner corrections through Raven's Slack receiver, adopted a human follow-up, produced a code/test patch, and finished the same task with three authorized decisions. A fresh MCP client subsequently reused the signed answer and learned owner, while still requiring a fresh signature.

The remaining problems are concentrated at the boundaries: recognizing that judgment is needed, having a usable authority map, presenting trustworthy evidence, avoiding repeated messages, and proving that the code implements the decision. Passing the authorization gate does not establish that last point.

This is an independent evaluation beside `evals/real_oss`, not a replacement for the other agent's Claude evaluation. It tests the shared authenticated HTTP/MCP path, with an agent credential, rather than a local unauthenticated database MCP process. No Raven production code was changed for this review.

## What actually ran

- Raven: [`1c56c79bf8ac2997121bc589fcc484a19b207e98`](https://github.com/nintendo-ds75/raven-preview/commit/1c56c79bf8ac2997121bc589fcc484a19b207e98), after resyncing the remote default branch, `main`.
- Public repository: [grafana/grafana](https://github.com/grafana/grafana), frozen at `d33bfe43b0ba6ec92b706f80d5e6d4b4d198fb5e`, September 17, 2026. The host received an exported snapshot without future git objects.
- Task: restricted users silently lose later resources when paging search-backed LIST results. Held-out reference: [PR #133017](https://github.com/grafana/grafana/pull/133017), merged September 18; reviewer `Engineer a3b078`. That reviewer is an observed technical participant, **not proven commercial authority**.
- Real host: Codex CLI `0.154.0-alpha.6.2`, default model `gpt-6-astra`, low reasoning effort as reported by its trace. Two independent processes, the second given recovery instructions and the same client key. The first was deliberately interrupted at its first wait.
- Raven data: 23,271 file paths, 1,500 pre-cutoff commits, 1,528 ownership listings, and eight prior public PRs. The public history sample has 198 human authors; the largest contributes 8.15% of human-authored commits, the top five 21.33%. GitHub's service committer is not counted as the human who did the work. This is distributed production development, not a single-author toy repository.
- Contacts: public pre-cutoff identities with **synthetic** Slack IDs. No private team membership or business authority was invented. A coordinator receives unresolved routes. The fixture intentionally tests what imported public evidence can do before a team configures verified decision owners.
- Real components: source investigation and editing, Raven ingestion/routing/storage, authenticated HTTP/MCP, durable outbox rendering, Slack reply parser and authorization, wait/resume, tree/follow-up, finish gate, and signed-memory reuse.
- Simulated components: people, their answers/referrals, and the Slack network transport. Referrals use the real person authorization check; answers use the real delivery receiver. No Grafana maintainer was contacted. Live Slack HMAC/events delivery and real human comprehension/response time were not tested.
- Raven's optional model retrieval and live lookup were disabled. This evaluates its supported deterministic path with a live coding host; it does not validate the optional semantic path. The host received workflow guidance, but no decision list, owner names, held-out diff, or answer key before simulated people replied.

The latest run took **292.6 seconds**, including restart. Its canonical task is `e83b4f0de586`. There was one task, two independently discovered nodes, one adopted human follow-up, three authorized active nodes, two initial coordinator referrals, four simulated reply attempts including one rejection, and eleven outbound root Slack messages. Fourteen MCP calls completed successfully; the interrupted wait is separately recorded in the actions.

## Each step: what worked and why users would still struggle

| Step and website promise | Observed in this run | Remaining design weakness and next change |
| --- | --- | --- |
| **1. Import the company map; keep it current.** | The right repository and code paths were found. CODEOWNERS teams and distributed history were present. | Public team handles are not reachable decision makers. Both initial decisions went to the coordinator. This is a safe abstention, but it falls short of automatic routing. Make verified team expansion, contacts, decision roles, fallback coverage, and sync freshness an explicit onboarding acceptance check. The eight PRs here are a sample, not a complete production connector sync. |
| **2. Understand the initial task.** | The host sent the full `goal`, paths and repo. Nevertheless kickoff returned `pass`: no clear owner/prior decision/pending question. | Missing ownership suppresses judgment engagement in the deterministic triage: the judgment-word and question branches require `people or owners`. Unknown ownership is not evidence that no judgment is needed. Separate decision risk from route availability; a risky unmapped task should engage the coordinator. See `bridge/canvas.py:300–311` at the tested revision. |
| **3. Discover the decision tree.** | Despite that pass, the host asked whether to fill pages across denied hits, then added a child concerning exhaustion and cursor failures. These are real compatibility/performance decisions, with code context and alternatives. | Success depended on a cooperative host following a fairly strong prompt. Raven did not propose the questions or check their coverage. A less cooperative host could create zero nodes and finish. Propose a small, evidence-linked set of candidate decisions from the task/diff, have the host accept or explain dismissals, and evaluate missed decisions separately from routing success. Do not count this single successful registration as a general reliability rate. |
| **4. Use what is known; distinguish predictions.** | The root first returned `predicted`, citing an Alerting search-endpoint PR and a generic compatibility default. It still required sign-off, correctly. | That precedent did not decide this pagination policy. Worse, the rendered Slack message called the prediction an answer from “the records.” The API's distinction is lost where the human acts. Keep evidence, assumption, and proposed answer separate in every channel; label the inference, surface the actual citation, and abstain when a retrieved record does not answer the question. `bridge/delivery.py:545–547` currently treats non-agent answers generically as records. |
| **5. Reach the responsible person with a useful question.** | Both first contacts were the coordinator, then referred to the held-out reviewer. Questions explained forbidden reads, compatibility, alternatives and extra searches. The follow-up routed straight to `Engineer a3b078` after learning the referral. | Initial exact-reviewer success was **0/2**. This is a cold-start setup limitation, not proof that configured authority maps fail. A large team match also does not identify its decider. In addition, the human message repeated options/context and truncated context at 600 characters. Present the concrete choice, consequence, relevant code/citation, and why this person can decide, with an expandable context link. |
| **6. Let people answer and correct asynchronously.** | Unsigned nodes blocked finish. The owner corrected the host's proposal, and the answer carried the actual signer. An answer to a reassigned thread was rejected; the simulator then answered in a fresh sign-off thread. | `reassigned` messages are judged stale whenever the node is already resolved, even when the new handoff message was just created for that resolved proposal. A human would be told to repeat an answer immediately after following the handoff. Choose the notification/reply contract from the current decision state, preserve revision checks, and consolidate changes into a stable conversation. Eleven root messages for three nodes is already noisy; this count excludes live Slack acknowledgement behavior. See `bridge/delivery.py:400–403`. |
| **7. Resume and expand the canvas.** | A fresh host used the same client key, recovered the same task, read the replies, and adopted the owner's empty-scan/missing-sort-field follow-up. Both completed waits returned changes in about four seconds. | Durability held. What happens next is still host behavior: the host must notice, understand, and incorporate the correction. The tree carries decision state; it does not show which code or tests changed because of that decision. Attach implementation/check evidence to each decision and expose unapplied or unverified corrections. |
| **8. Implement the signed decision.** | The host changed the real LIST implementation and added focused Go tests for denied rows, inexact totals, limits, resource versions, missing cursors, and later errors. Static review confirms these branches are present. | The patch loops across backend searches; the historical fix returns a continuation token after one search batch. The simulated answer left room for interpreting “continue” either way, so this is **not proof of disobedience**. It is an unverified behavior/performance difference: one LIST can scan many more rows. Require explicit acceptance criteria for work bounds and response behavior, and show a decision-to-diff review. Approval of prose cannot establish equivalence. |
| **9. Finish with confidence.** | Raven finished with three signed nodes. `git diff --check` passed. The host explicitly reported that Go and gofmt were unavailable. | The result is a reviewable patch, **not a verified Grafana fix**: it was not compiled or executed. Raven has no evidence that the signed behavior holds in code and no visibility into test success. Distinguish “decisions authorized” from “implementation verified”; for pilot changes, require CI/test evidence and normal PR review before claiming readiness to ship. |
| **10. Reuse knowledge on the next task.** | A fresh authenticated client on a copied database asked the original question in a new task. Raven returned the exact signed answer, cited its decision ID, chose the learned owner, and refused finish pending a fresh signature. | This exact-match reuse is useful and safe. It does not establish reliable distant paraphrase retrieval, changed-fact handling, or general understanding of how a person decides. Keep scope/facts/expiry visible and evaluate reuse precision and harmful transfer independently. |

## What to fix before inviting the first team

1. **Make the first installation succeed.** A guided setup should finish with reachable named decision owners for the pilot's paths, a working coordinator, and a real notification/reply smoke test. History alone is insufficient in this fixture.
2. **Close the discovery gap.** Unknown owners must not imply `pass`. Add candidate decisions and an explicit coverage review for changes to compatibility, permissions, schema, defaults, and rollout. Measure missed decisions rather than just completed canvases.
3. **Make the owner's interaction trustworthy and quiet.** Correct the prediction-as-records label and the resolved-node reassignment reply failure. Preserve stale-signature protection while avoiding redundant fresh threads.
4. **Separate permission from implementation proof.** Keep code review and CI in the pilot workflow. Link each important decision to affected changes and acceptance checks; do not represent a signed tree as proof that the agent followed it.

Those are the first-user priorities. More connectors or broader prediction coverage do not resolve them. An assisted, single-team pilot with configured ownership is a reasonable next step; the current evidence does not justify offering hands-off autonomous delivery.

## Verification and limitations

- Latest full Python suite: **360 tests, OK, four optional SDK tests skipped**. Three additional measurement tests pass.
- Authenticated HTTP concurrency probes: draft hidden, draft blocks finish, overlapping same-client-ref requests return one node, and an add arriving after completion is rejected — **all pass on `1c56c79`**. The duplicate-node result from `9bf5d49` is fixed; it is not a remaining finding.
- Existing managed-agent browser suite passes using installed Chrome.
- Existing general browser suite **fails** at `tests/browser.cjs:176`: `owners overflows on mobile`, at a 390-pixel viewport. I cannot confirm “both browser suites green” in this environment. This is a smaller usability problem than discovery or implementation verification. The log is included.
- No Go compilation/test execution, real Slack delivery, live people, Teams replies, production-scale service load, or complete GitHub team sync was exercised. Shared Raven used SQLite and loopback HTTP; this is not a production deployment test.
- This is one task and a prompted host, without an equal-budget no-Raven arm. It demonstrates a workflow and specific failure modes, not a productivity improvement or a statistical success rate. The held-out labels and simulated answers can assess routing recovery and decision handling, not actual organizational authority.
- Earlier runs on `9bf5d49` are not counted in these results. The first calibration pass exposed Raven's draft visibility race as well as a simulator event-id retry bug. Codex quota failures, an unsupported Claude CLI flag, and expired Claude OAuth also interrupted setup attempts. The reported task is the fresh successful `1c56c79` run; no failed attempt is represented as a completed test.

## Evidence and reproduction

Start with `results/main-1c56c79/metrics.json` (not included in this preview), `mcp-calls.json` (not included in this preview), `slack-messages.json` (not included in this preview), `host.patch` (not included in this preview), and `contract-probes.json` (not included in this preview). The initial/final trees, host command excerpts, interruption, simulated actions, reuse probe and test logs are adjacent. Long strings are explicitly bounded with full-value hashes; databases, tokens, and CLI transport metadata are excluded.

```sh
git clone --filter=blob:none --no-checkout --single-branch https://github.com/grafana/grafana.git /tmp/bridge-grafana
git -C /tmp/bridge-grafana fetch --depth=1700 origin 39703a2c01e37d6ef9f91147d1db29dd16c82611
python3 -m evals.real_oss_remote.fetch --out /tmp/bridge-grafana-public
python3 -m evals.real_oss_remote.prepare --repo /tmp/bridge-grafana --public /tmp/bridge-grafana-public --out /tmp/bridge-eval-fresh
python3 -m evals.real_oss_remote.run_host --prepared /tmp/bridge-eval-fresh --case storage-pagination --host codex --restart --timeout 660
python3 -m evals.real_oss_remote.probe_contract --prepared /tmp/bridge-eval-fresh --out /tmp/bridge-contract-fresh
python3 -m evals.real_oss_remote.probe_reuse --source /tmp/bridge-eval-fresh/runs/storage-pagination --out /tmp/bridge-reuse-fresh
python3 -m unittest evals.real_oss_remote.test_driver
python3 -m evals.real_oss_remote.collect --prepared /tmp/bridge-eval-fresh --case storage-pagination --out /tmp/bridge-review-export
```

Use a fresh output directory; existing run databases are intentionally not overwritten. Requires Python 3.11+, Git, network access for public data and host inference, and an authenticated host CLI. Public GitHub unauthenticated API limits apply. The four task definitions are available for future runs; only storage pagination was rerun on this final revision. `--host claude` is supported but was not the successful host in this environment. The selected public PRs fit in one API result page; the small fetcher is not a general PR crawler.

The included patch is a review artifact against Grafana source, not applied to Raven or submitted upstream. Grafana's source is AGPL-3.0; see the [license at the frozen revision](https://github.com/grafana/grafana/blob/d33bfe43b0ba6ec92b706f80d5e6d4b4d198fb5e/LICENSE).

## Addendum: the deployment and discovery probes

A later review of the Docker and PostgreSQL stack found three more
things, two of which reproduce with no Docker and no host at all. They
are reproductions, kept next to the report that found them:

```sh
python3 -m evals.real_oss_remote.probe_deployment   # the printed address, and effective authority
python3 -m evals.real_oss_remote.probe_discovery    # where the kickoff verdict came from
```

- **`./setup --yes --port 17433` printed a `127.0.0.1` URL and configured
  `localhost`, and the printed URL answered 403.** Docker publishes
  `BRIDGE_PORT` while the container always binds 7333, so
  `BRIDGE_PUBLIC_URL` is the only thing that tells the server which port
  people reach it on. Setup now keeps the two together, `./dev up` prints
  the address the server was configured with and warns when they have
  drifted, and both spellings of a published loopback address are
  accepted: refusing one of them protected nothing.
- **Readiness dropped its missing-authority blocker for authority
  recorded against an unrelated repository, or already expired.** It now
  reads authority per ingested repository, counting only rows that are
  accepted, in force today, and say the person decides or approves.
- **The kickoff verdict was read from the host's own boilerplate.** The
  original Grafana brief got `pass`, and the run engaged only because
  the appended workflow instructions contained "authorization" and
  "release". A labelled block of house rules is set aside now, a
  sentence addressed to the agent that names nothing in the repository
  is not read as a statement about the change, and the engage reason
  quotes the words it read. The brief itself engages on its own subject:
  registering a feature toggle is somebody's call about what states it
  takes and what it ships as.

Both probes exit non-zero on the unsafe outcome, and each defect also has
a rejecting test in `tests/` (`test_auth.py`, `test_setup.py`,
`test_authority.py`, `test_canvas.py`).
