# Pytest replay pilot

The first deterministic canvas replay against pytest found useful failures:
all three tasks received `pass`, and four of six diagnostic questions were
unrouted. The node-ID case routed to Ronny Pfannschmidt, an actual approver.
This is a curated smoke test, not an estimate of general accuracy.

After removing the undo-keyword retrieval veto, the original monkeypatch
question resolves with a citation to PR #14209 and remains marked for
sign-off. The broader question about delattr/setitem/delitem stays unrouted;
its trace now reports a relevance failure instead of mistaking it for a
reversal. The other cases are unchanged. See
`after-retrieval-fix.json` (not included in this preview) and the
gate audit (internal planning document). The original baseline below is
preserved. Complete new responses are in
`.bridge/pytest-replay/after-retrieval-fix/results.json`.

The subsequent merge-weighting change (internal planning document) gives all
six nodes owners while retaining the same questions and path discount.
Both TOML questions identify Ran Benita, and the node-ID questions still
identify Ronny Pfannschmidt. The monkeypatch route is Ran, a plausible
alternative rather than the actual replay PR's approver. See
`after-merge-weighting.json` (not included in this preview). Kickoff still
passes all three tasks; this is not an end-to-end agent evaluation.

## Cases and observed baseline

Recorded September 16, 2026 against Raven commit
`b2c3b0793e19d9fb008d57908120f8f2f8b8dcba`. Full compact evidence is in
`baseline.json` (not included in this preview); inputs are in [`cases.json`](cases.json).

| Case | Kickoff | Two diagnostic probes | Actual approving reviewers |
| --- | --- | --- | --- |
| [14910: monkeypatch failed mutations](https://github.com/pytest-dev/pytest/pull/14910) | pass | both unrouted | RonnyPfannschmidt |
| [14954: native TOML max_warnings](https://github.com/pytest-dev/pytest/pull/14954) | pass | both unrouted | bluetech (Ran Benita), Pierre-Sassoulas |
| [14758: structured node IDs](https://github.com/pytest-dev/pytest/pull/14758) | pass | behavior question pending with Ronny; accountability question resolved to Ronny | RonnyPfannschmidt, bluetech |

Six retries returned the original node IDs. All three target merge/squash
commits were verified not to be ancestors of their respective input bases.
No behavioral question was resolved from prior records. The one resolved
answer identified an owner; it did not approve the implementation.

## What this exposed

1. **Kickoff wording sensitivity.** The compatibility and refactor tasks
   explicitly preserve existing behavior and plugin interfaces but receive
   `pass`. The deterministic triage uses a policy-word regex and cross-area
   ownership listings. These snapshots contain no indexed MAINTAINERS or
   CODEOWNERS listings. Discovery's candidates alone do not trigger
   cross-area engagement. A pass still permits an agent to add questions.
2. **Discovery and routing disagree in confidence.** The TOML kickoff names
   Ran Benita as owning the area and includes Pierre Sassoulas among its
   candidates, both actual approvers. Subsequent questions are unrouted
   because the routing threshold finds no clear owner. This is an abstention,
   not evidence that these people were absent from the graph.
3. **“Undo” is mistaken for reverting a change.** Both monkeypatch questions
   hit the blanket revert-question gate in `bridge/ladder.py`. That prevents
   record retrieval even for the explicit reference to the older setattr
   fix, #14209. Its commit `47ed628df` exists before the case's base.
4. **One useful route.** Ronny is selected for the cross-area node-ID question,
   with prior acceptance, authorship and blame signals. Public reviews show
   he approved the actual change. This is one case, not routing precision.

The TOML review is a particularly useful future end-to-end test: review
identified changed `getini()` behavior for unset, INI and override values;
the final change preserved those while accepting native integer zero.
The question in this pilot already supplies that compatibility concern.
It does **not** demonstrate that an agent or Raven discovered it.

## Reproduce

From the Raven checkout, with Python 3.10+ and git:

```sh
git clone --single-branch --branch main https://github.com/pytest-dev/pytest.git .bridge/pytest-replay/source
python3 -m bench.pytest_replay.run \
  --repo .bridge/pytest-replay/source \
  --out .bridge/pytest-replay/my-run
```

Skip cloning if the checkout exists. Choose a new output directory each
time. The runner refuses to reuse one, stores one database per case, and
writes complete task/node/tree responses to `results.json`. No dependencies,
model keys, maintainer messages or upstream writes are required.

The local pilot's databases and full responses are in
`.bridge/pytest-replay/run-03/`. Public PR metadata, changed-file patches,
reviews and issue comments were collected separately under
`.bridge/pytest-replay/heldout/`; the runner never reads that directory.

## Scope and limits

- Inputs are manually written problem summaries and diagnostic questions,
  informed by public issues and completed reviews. They are not blinded,
  original issue snapshots or independently agent-generated trees.
- Each graph is pinned to the target merge's first parent (the squash
  parent for #14758), with the standard 2,000-commit ingestion window and
  separate merge-history window. Git signal reads use that revision.
  Other changes accepted before that base are available.
- The source clone physically contains later history. Raven's reads are
  revision bounded; this checkout is **not** an isolated agent sandbox.
- Model rungs and live record retrieval are disabled. No GitHub discussion
  text is ingested. Current public review data is evaluation evidence,
  not an authenticated source of owner sign-off in Raven.
- All six questions are deliberately probed, even after `pass`; they do
  not measure how often a real agent would interrupt. Probes are sibling
  nodes, not a discovered dependency tree.
- Requesters use the original human identity where available. #14954 uses
  PR author Engineer f59198; its git patch author was a coding-agent identity.
- No independent coding agent, implementation comparison or pytest test
  suite was run. Those remain the next stage of the proposed end-to-end
  evaluation. The present run tests ingestion, kickoff, resolution, routing,
  tree persistence and retry behavior on real repository history.

For the next stage, use isolated pre-change clones, give the agent only
problem statements, and keep completed reviews outside its accessible
workspace. Score questions discovered, unnecessary interruptions, cited
answers, owner choices and implementation/test outcomes separately.
