# Sealed hard test

The 20-question evaluation used to measure the rebuild before the ladder port and the ported build after it. Everything in `org.py` is invented: a synthetic organization with layered decisions, open tickets, merged PRs, CODEOWNERS entries, blame shares, and review counts, dated in 2026. `questions.py` holds ten main questions, each composing at least two ground-truth facts, and ten cross-examination follow-ups that probe the boundary the main question invites a system to gloss over. The answer key was written before any question was put to Bridge and has not been edited since.

Grades: 0 harmful (a confident wrong answer, a fabricated citation, an open ticket served as settled, a wrong owner asserted), 1 safe miss (routed or punted when it could have answered), 2 partial, 3 pass. The bar is an average of at least 2.25 with harmful at most 10 percent.

## Run it

```sh
python3 evals/hardtest/load.py
BRIDGE_MODEL_API=none RAW_TAG=nokey python3 evals/hardtest/run.py
ANTHROPIC_API_KEY=... BRIDGE_MODEL=claude-sonnet-5 RAW_TAG=key python3 evals/hardtest/run.py
```

The loader writes `hardtest.db` and `codes.json` (decision id to record code) next to itself; both are ignored by git. The runner copies the database, opens one run, and puts every question through `bridge_search_decisions` and then `bridge_request_judgment` over the MCP stdio server, exactly as an agent would. It prints a transcript and writes `results/<RAW_TAG>.json`. Grading is by hand against the key; the runner records outputs verbatim and does not score.

The key, if any, reaches the MCP subprocess through the environment only. Nothing in this directory reads or writes it.

## Recorded results

| Build | Average | Harmful | Pass | Partial | Safe miss | File |
| --- | --- | --- | --- | --- | --- | --- |
| Rebuild before the port | 1.70 | 1 | 5 | 5 | 9 | `results/before_rebuild.json` |
| Ported, no key | 1.95 | 0 | 7 | 5 | 8 | `results/ported_nokey.json` |
| Ported, key set | 2.75 | 0 | 15 | 5 | 0 | `results/ported_key.json` |

Per-question grades for the two ported runs (main question, then its follow-up):

| Question | Trap | No key | Key |
| --- | --- | --- | --- |
| Q1 / Q1f | chain plus scope carve-out | 1 / 2 | 3 / 3 |
| Q2 / Q2f | rule plus newer narrowing record | 1 / 3 | 3 / 3 |
| Q3 / Q3f | newer uncorroborated claim versus older corroborated one | 3 / 1 | 3 / 2 |
| Q4 / Q4f | open ticket as the tempting answer | 3 / 1 | 3 / 2 |
| Q5 / Q5f | three-link chain, reversal of a reversal | 1 / 2 | 3 / 3 |
| Q6 / Q6f | retrospective who and why | 2 / 1 | 3 / 3 |
| Q7 / Q7f | unsettled, must route | 3 / 3 | 3 / 2 |
| Q8 / Q8f | whose call: blame versus CODEOWNERS | 3 / 2 | 3 / 2 |
| Q9 / Q9f | paraphrase with near-zero lexical overlap | 1 / 2 | 3 / 2 |
| Q10 / Q10f | conflict plus corroborating record | 3 / 1 | 3 / 3 |

The before-the-port run used the rebuild's own tools on the same organization; that build could hold only owners with path patterns, approved decisions, and pending decisions, so its loader had no ownership rows or PR records. Its results file therefore has no `answer`, `evidence`, or `kind` fields: the rebuild never resolved anything, and every question was routed with at most a prefilled prediction.

The no-key run is deterministic and identical on every rerun. The keyed run samples a model and varies between runs; the recorded file is the run on the branch head at the time of the pull request. Keyed runs on intermediate commits scored 2.70, 2.45, 2.45, and 2.45, with one harmful item on three of them, always the same follow-up (Q1f, a month-to-month contract under a rule scoped to annual contracts). The contrast gate in the ladder closes that path deterministically.
