# Live source loop evidence

See the [report](../../../docs/live-source-loop-2026-10-06.md) for the rubric,
interventions, failures and limits. Product revision: `68b98b8`. Times in the
JSON are UTC; the local test took place on 6 October 2026 in Los Angeles.

* `host-summary.json` records each process outcome, cost and limit. `host-*-mcp.json`
  contains the corresponding assistant text and MCP exchanges, with private
  reasoning and unrelated file-read outputs excluded. These are four processes,
  not four independent passing tasks.
* `task.txt`, `resume-a.txt`, `task-b.txt` and `history.txt` are the actual prompts.
* `repository.json` records the public checkout and contribution distribution.
* `slack.json` and `human-events.jsonl` record the synthetic transport and human
  replies. Local bearer credentials are replaced with `[local-test-credential]`.
* `source-v*.json` and `source-import-v*.json` show policy changes and effects.
  `cache-b-before-change.json` demonstrates unapproved reuse and the contradictory
  provenance wording. The two `*-after-source-change.json` files capture revocation.
* `browser-stale-review.txt` captures the refused stale form. The screenshots show
  the human interaction surfaces, including the signed current source revision.
* `proof-v1-final.json` and `proof-v3-final.json` are the immutable exports after
  refreshed finish calls. Only version 3's `bundle.payload.change.diff` matches
  its independently captured `patch-v3-final.diff`. Version 1's submitted diff
  lost three context spaces; it is preserved in `patch-v1-submitted.diff`.
  `patch-application.json` shows that the actual v1/v3 patches apply to the pinned
  baseline and v1's submitted patch does not. `proof-a-after-b-reapproval.json` preserves the earlier
  bundle while reporting it stale. `verification.json` records exact comparisons.
* `upstream-before.log`, `upstream-final.log` and `oracle-*.log` are native and
  independent verification. `oracle.py` is the seven-group evaluator oracle.
* `pseudonyms-*.log` covers only the fixture helper changed in this commit;
  `context-browser.log` covers the nine focused browser checks.
* `restart-wait.json` returned immediately. It is deliberately retained to show
  that this was not a successful test of interrupting an in-flight wait.

No real Slack workspace or Jira pull connector was used. Human identities are
synthetic; public git contributor identities were pseudonymized before inference.
No credentials, raw local configuration, databases or private reasoning are
included. This evidence is specific to one supervised scenario, not an overall
accuracy benchmark.
