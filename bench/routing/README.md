# Routing replay bench

Historical replay of who-to-ask routing on real repositories. No API is
used: every label comes from git.

## Protocol

For each sampled commit C (at least 150 per dataset, spread over time):

1. T is C's committer date, the moment the change was accepted. Everything
   at or after C is hidden: Raven sees the repository as of C's parent.
2. The question is the decision C was making, from C's subject and body
   through a deterministic template ("Should hw/riscv add the imsics
   compatible string?"), or with `--questions model` through one cached
   model call per item. The commit body's first paragraph is the context.
3. Raven is asked in the accountability form ("Who should approve this
   change: ...") under four conditions:
   - WARM: a fresh database ingested from the repository as of the parent
     revision (history, tree, MAINTAINERS and CODEOWNERS at T).
   - COLD: an empty graph that knows only where the checkout is and the
     revision bound; Raven fetches static context at ask time.
   - PATHLESS: every file path, filename and directory name known to the
     tree at T is stripped from the question and the context.
   - PATHLESS-COLD: both.
4. Labels: strong = the people in Reviewed-by, Acked-by and Helped-by
   trailers, the committer who accepted the patch when they are not its
   author (never a catch-all integrator who commits more than half of
   everything), or the human who authored the merge commit of a pull
   request. Listing = the MAINTAINERS (M: and R:) or CODEOWNERS humans for
   the touched paths at T, a second and weaker label. Team handles in
   CODEOWNERS are recorded but are not people.
5. Related people = everyone who authored, committed, reviewed or merged
   into the touched files' own directories before T (last 400 changes).

`--stale` adds a series that ingests once at an early item and asks every
later item without re-ingesting, bucketed by months since ingest.

## Metrics per condition

- top-1: the routed owner is a strong label.
- top-3: any of the top three candidates is a strong label.
- alternate: the routed owner is a strong or listing label.
- wrong person: routed to someone who is not labeled, not listed, and not
  related to the touched paths (the harmful class).
- unknown (should know): no owner although a strong label was related to
  the paths or listed at T.
- unknown (honest): no owner and no label was knowable from pre-T data.
- evidence honest: the first evidence line names a real signal (a share of
  changes, a listing, review history, a referral) about a related path,
  never a composite score or a stray path token.

Every miss is classified by mechanism (repo-wide fallback, wrong area,
listed but not the approver, stale listing, inactive person, bot, team
handle, authorship over review, plausible reviewer not labeled, no
relation, unknown) and the per-dataset JSON keeps every question, answer,
evidence line and score.

## What the labels are, and are not

A reviewer, an acker or a merger is a proxy for technical involvement, not proof of commercial or operational decision authority: the bench measures who a change was accepted by, which is the best label public history carries, and not who could authorize a pricing or policy decision. It has none of a team's private context, policy exceptions, referral history or response delays. Everything at or after the sampled commit is hidden, so no future answer, membership change or ownership change reaches the pre-task evidence; a dataset is one repository, and the held-out sets are never inspected while fixing. Read it as routing under realistic history, aliases and stale listings, and read the acceptable-contact rate apart from the authorized-signer rate; a production decision database is what the pilot's own evaluation measures.

## Baselines, separate rates, strata

Every run scores three baselines on the same items, each from one signal and no ladder: `baseline-codeowners` (the first human the listing names for the touched paths), `baseline-recent-reviewer` (whoever reviewed, accepted or merged in the touched directories most recently before T, never the author) and, when `BRIDGE_BENCH_MANUAL_MAP` names a JSON file mapping path prefixes to people, `baseline-manual-map`. The summary reports, apart from the headline metrics, coverage (somebody named at all), precision among routed, acceptable contact among routed (a label or a listing) and authorized signer (a strong label whose approval counted), and per condition the same numbers by stratum: team-only listings, labels without trailers (the committer or merger alone), aliases (a label known by another name than history uses), new files (nobody touched them before) and stale listings (the listed people inactive for a year at T).

## GitHub-flow labels

A squash-merged repository carries no trailer; its approvers are known to GitHub. With `BRIDGE_BENCH_GITHUB_DB` pointing at a Raven database that synced the repository (`bridge sync owner/name`), the approving reviewers and the merger of the pull request a commit merged are added to the strong labels, by the name Raven knows them by.

## Datasets

| name | source | labels | listing | role |
| --- | --- | --- | --- | --- |
| qemu | qemu/qemu | Reviewed-by, Acked-by, accepting committer | MAINTAINERS | tune |
| linux-drm | torvalds/linux, drivers/gpu/drm via sparse checkout | Reviewed-by, Acked-by, accepting committer | MAINTAINERS | tune |
| node | nodejs/node | Reviewed-By, lander | CODEOWNERS (teams only) | tune |
| git | git/git | Reviewed-by, Acked-by, Helped-by | none | held out |
| nixpkgs-nixos | NixOS/nixpkgs, nixos/ via sparse checkout | the human who merged the PR | ci/OWNERS (CODEOWNERS syntax) | held out |

The held-out sets are never inspected while fixing; only their summary
numbers are recorded per iteration.

## Running

```sh
export BRIDGE_BENCH_SCRATCH=~/.cache/bridge-bench   # clones, item caches, work dirs
python3 -m bench.routing.replay --datasets qemu,linux-drm,node --n 150 --stale --out bench/routing/results/my-run
python3 -m bench.routing.replay --datasets git,nixpkgs-nixos --n 150 --out bench/routing/results/my-run-holdout
```

Options: `--limit K` for a smoke run, `--jobs N` workers, `--conditions
warm,cold`, `--questions model --model <id>` (needs `ANTHROPIC_API_KEY`
in the environment; questions are cached under the scratch dir), `--seed`,
`--model-rungs` (the model-backed rungs run: the fast model maps pathless
questions to areas; the summary says so; needs the key in the
environment). Every ask passes the commit's author as the requester, who is
never routed to. Clones are shallow (`--shallow-since`), blobless and
sparse for the kernel and nixpkgs, and cached. The warm database for
each base is cached under the scratch dir keyed by the ingest code, so a
rerun pays no ingest; the cache drops databases from an older ingest
first and the least recently used after, staying under
`BRIDGE_BENCH_WARM_CACHE_GB` (default 4). A run writes
`<dataset>.json` (every row), `summary.json` and `summary.md`; the
repository keeps the summaries.

## The tree exam

`tree_exam.py` replays a merged single-author series (2 to 8 commits
inside one merge) as one task on the canvas: `bridge_start_task` with the
first subject as the title, the first body as the goal, the series author
as the requester and the touched directories as the paths, then one
`bridge_add_node` per commit, each grown from the previous node, with the
commit's subject as a decision ("Should X do Y?") and its body as context.
Two conditions: `tree` and `tree-pathless` (every path stripped from the
node questions; the kickoff paths stay).

Per node the routing bench's metrics apply against that commit's
approvers (nodes git names no approver for are counted as asks, not
scored). Per task: asks, distinct people asked, people precision and
recall against the approvers of the whole series, nodes deduplicated. The
kickoff verdict is reported against whether an approver other than the
requester was required and whether the series crossed areas with
different listed owners; in these projects a maintainer approves nearly
every series, so the first label is almost always true and the verdict is
conservative by design.

```sh
python3 -m bench.routing.tree_exam --datasets qemu --n 50 --out bench/routing/results/tree-run
python3 -m bench.routing.tree_exam --datasets git --n 50 --out bench/routing/results/tree-run-holdout
```
