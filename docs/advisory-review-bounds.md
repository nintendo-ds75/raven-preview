> **Experimental archive: semantic effectiveness remains unproven. Do not merge or deploy.**
> See the [validation status](advisory-review-experiment-validation.md). Use `main` for current validated code.

# Bounded advisory review

Raven's diff reading is advisory. Authorization remains the finish gate; a model
reading is neither a test run nor a proof of correctness.

Each reading examines the signed decision, the supplied diff, and the other
signed decisions in context. Its three stages are requirement decomposition,
counterexample search, and extra-policy-condition search. The counterexample
stage makes one focused pass over every numbered requirement and the shown
relevant paths. It must consider temporal history, including permitted state
changes between inputs, rather than equating current membership with once-only
processing. It stops searching a requirement once it has a concrete
counterexample and continues examining the remaining requirements.

## Compact output, explicit uncertainty

Every stage returns `status: complete` or `status: inconclusive`, plus a short
`unexamined` limitation when needed. Completion attests that the stage examined
its whole supplied scope; omitted entries in a sparse counterexample response
are not implicitly assumed to pass without this attestation.

The first stage still lists each requirement. The critic's `coverage-v2` response
separates compact coverage from actionable findings:

- `checks` accounts for every numbered requirement with `n` and `assessment`:
  `honored`, `unseen`, or `alleged`. These rows never consume the finding allowance.
  Include `at` when explicitly clearing an unsupported first-reader allegation.
- `exits_checked` accounts for every listed early-exit index.
- `findings` holds at most **four distinct actionable findings in total**, including
  early-exit findings. Each has a requirement `n`, an exact code quote `at`, and a
  structured `allegation` or missing-code detail `not_shown`. Exit findings also
  identify `exit`. The separate extra-conditions stage still allows four conditions.

Ten or more successful coverage rows therefore do not themselves force an
incomplete result. `complete` requires all indices, the explicit completion
attestation, a bounded response, and no malformed, contradictory or omitted
findings. `unseen` remains semantically unknown even after a completed examination.
An `alleged` coverage row must have a corresponding actionable finding. An
ungrounded allegation remains unclear; its presence is not a validated departure.

Identical duplicate coverage and findings differing only in surrounding whitespace
are idempotent. This is textual deduplication, not proof that two traces are semantically equivalent. Conflicting duplicate assessments, missing or unknown indices, and
positive coverage paired with negative findings are incomplete. All candidates
are examined before retention: grounded witnesses take priority, including late
contradictory evidence. Overflow marks the whole critic incomplete and discloses
omissions, never treating omitted evidence as a pass. Multiple retained witnesses
or missing-code details for one requirement remain inspectable in bounded
`counterexamples` or `not_shown_details` lists, alongside their primary fields.

The response must fit 6,000 normalized JSON characters. Retained critic evidence
has a separate conservative 6,000-character allowance and a four-finding cap;
per-field limits still apply. At most six limitation summaries are presented,
with an explicit omission count when clipped. Malformed ancillary fields cannot
erase independent valid evidence, but do prevent completion. Bounded observer
snapshots may retain detailed controlled parser diagnostics; historical readings
that did not capture them cannot be reconstructed.

The low-level parser retains unversioned legacy sparse complete-attestation
semantics for old response shapes. New production readings additionally require
source bindings and explicit whole-source coverage, so a legacy sparse reply
cannot complete a current reading. Valid grounded partial evidence can survive. Prompts request short labels,
exact short code fragments, and no analysis transcript or narratives for successful
checks. If scope or findings do not fit, the response must be inconclusive.

A normalized JSON response over 6,000 characters, a missing completion status,
malformed rows, or overflowing finding collections cannot support an all-clear.
Independently valid findings from a partial reply are retained. Unknown code,
unlocated counterexamples, and unread portions of a long diff remain explicit
uncertainty. Existing concrete departures are not erased because another part
of the reading was incomplete.

## Budget rationale

The existing requested answer target remains **1,200 tokens per stage**. Four
short findings and exact short quotes generally fit a compact response. The
6,000-character guard is a separate conservative response-shape limit, not an
assumption that characters map to a fixed number of tokens. If the model cannot
fit a complete reading, that limitation is reported.

The existing **8,000-token thinking allowance is unchanged**. A model discovered
to use thinking still gets its one initial retry with that allowance; a known
thinking model starts with it. Total provider output usage includes thinking,
so a 9,200-token response does not mean 9,200 tokens of visible JSON.

Advisory stages no longer automatically double their answer budget after
truncation or rerun an entire reading to repair malformed JSON. After the
existing one-time thinking discovery, an exhausted stage is inconclusive. This
removes the repeated 9,200/10,400-token search pattern seen during evaluation
without increasing model budgets or removing adversarial review. Non-review completion retry behavior is unchanged.

The production HTTP timeout remains **120 seconds** and its existing transport
retries remain unchanged. Claude CLI execution keeps its existing process
limit. These are not hard end-to-end latency guarantees: transport retries and
thinking discovery can still take time. Prompt-level concision must be checked
with actual-provider runs; deterministic tests cannot demonstrate faster or
more accurate model reasoning.

## Persistence and recovery

Incomplete stage results carry `incomplete: true`. Saved review attempts keep
valid partial findings but have status `failed`, so an explicit finish call with
the same diff can retry once. Merely reading stored state does not start another
model call. The evidence bundle preserves the earlier incomplete attempt after
a later retry succeeds. A fully completed reading with an honest semantic
`unclear` verdict is still a completed advisory result, not a transport failure.

Run deterministic checks with:

```sh
python -m unittest discover -s tests -p 'test_bounded_review.py' -v
python -m unittest discover -s tests -p 'test_review_invariants.py' -v
python -m unittest discover -s tests -p 'test_review_coverage.py' -v
python -m unittest discover -s tests -p 'test_proof.py' -v
python -m unittest discover -s tests -q
```

These tests use synthetic response bodies, sanitized truncation metadata,
executable temporal controls, and the actual parser/persistence paths. They do
not call a provider or establish real-model semantic reliability.

## Sonnet 5 effort experiment

A production-timeout reread still exhausted 9,200 total output tokens in the
buggy case's critic before returning any visible text. Its temporal detection
remains unestablished. Compact JSON alone does not bound adaptive thinking.

For bounded advisory calls only, the exact Anthropic model ID `claude-sonnet-5`
now requests `output_config: {effort: "medium"}`. Other purposes, model IDs and
backends retain their previous request shape. Adaptive thinking stays enabled
by default; token limits and the HTTP timeout do not increase. This is an
experiment to reduce overlong reasoning, not a guaranteed visible-output
reserve or an established improvement in detection.

For the same Sonnet 5 API path, the critic is instructed to examine every
numbered requirement using at most three selected candidate traces each, then
inspect each listed early exit once. It settles a
trace without repeatedly revisiting it and emits the structured result after
that pass. Specific relevant paths still unresolved at the limit produce an
inconclusive result. This finite search is advisory rather than exhaustive.

Official compatibility sources checked on 6 October 2026:

- [Sonnet 5 overview](https://platform.claude.com/docs/en/models/sonnet-5/overview):
  `claude-sonnet-5` uses adaptive thinking by default, with high default effort.
- [Effort](https://platform.claude.com/docs/en/build-with-claude/effort):
  top-level `output_config.effort` supports medium on Sonnet 5 without a beta
  header. Effort is a behavioral signal, not a strict token budget.
- [Thinking configuration](https://platform.claude.com/docs/en/build-with-claude/thinking#configuring-thinking):
  manual `enabled`/`budget_tokens` and `between_tools` are unsupported on Sonnet 5.
  Disabling thinking is supported, but this experiment does not do so.
- [Steering and cost control](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost#cost-control):
  `max_tokens` limits total thinking plus response output; lowering effort can
  leave more room for text but does not guarantee an allocation.
- [Task-budget support](https://platform.claude.com/docs/en/build-with-claude/task-budgets#feature-support):
  the task-budget beta does not support Sonnet 5 and is not used here.
- [Sonnet 5 prompting](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/prompting-claude-sonnet-5#calibrating-effort-and-thinking-depth):
  medium trades some capability for cost, while low risks under-thinking on
  moderately complex work. Quality and latency must be measured on the actual
  review workload before drawing a reliability conclusion.

These model-specific controls must not be inferred for unverified aliases,
older models, or newer versions. Offline tests verify the request contract and
continued handling of incomplete responses; they do not validate real-model
accuracy or performance at medium effort.

## Behavioral allegations and equivalent implementations

A code location and a negative verdict are insufficient evidence for a claimed
behavioral departure. The reader must provide a bounded allegation containing:

- An exact, relevant quote from the signed answer
- A permitted input and initial state
- An ordered sequence, including any intervening state changes
- Expected behavior under the approval and different observed behavior
- A production-code location for that behavior

Raven checks field types and bounds, the approval quote, the code location, and
whether the expected and observed descriptions differ. It does not execute the
submitted diff or independently prove the allegation. A structured witness is
still a model's advisory claim; a false expected/observed description can fit
its schema. Behavioral examination and tests remain necessary.

Unsubstantiated negative labels become unclear, and their free-text reasons
are not repeated as established departures. The critic may supply a grounded
witness or explicitly reject an unsupported first-reader allegation. A missing
witness cannot be cleared by omission. Valid partial allegations survive a
malformed or incomplete neighboring finding.

Behavioral words such as once-only or ignore duplicates do not require a
particular history container. Correct set-based and mapping-based implementations
are acceptable under the same policy. Structural allegations are separate:
they must name a literal identifier, path, signature, type or value actually
present in an explicitly structural approval quote. Ambiguous structure claims
remain unclear rather than creating an implementation preference.

### Reviewer-contract cache boundary

New review keys include `approved-source-v3` as well as the task, exact
diff identity and signed-answer revisions. An explicit finish therefore starts
a current-contract reading instead of reusing a completed earlier-schema reading,
including `behavioral-allegations-v1` and `behavioral-coverage-v2`.
Repeated explicit finishes under the same contract still share that reading.

New events retain their contract version. Existing unversioned events are shown
as legacy; read-only current views flag an older contract and explain that an
explicit finish is needed to reread it. Reading a tree or exporting an existing
proof never starts inference. Previously saved proof bundles and historical
review events are not rewritten. Their old all-clear remains historical
evidence of what the old reader said, not validation under the new contract.

### Three-control examination

A useful next live check compares the original buggy change with both a correct
set-based control and an equivalent mapping-based control. The replay must
inspect a permitted state-change trace, not infer detection from a located
negative label. A complete structured candidate plus accepted controls requires
manual witness adjudication; the harness must not call that an automatic
semantic pass. Cost/request reservation and explicit operator execution are
required before another live run. No live effectiveness claim follows from the
schema, request-body or trusted-reference-function tests.

### Coverage-schema evaluation boundary

The live v2 three-control reading returned 11 critic checks for the buggy control
and 10 for the equivalent mapping control. The old parser incorrectly applied
its four-finding bound to those check collections. That run retained no normalized
critic rows or witness, so the contents of the rejected rows are unknown. The
coverage/finding split repairs that deterministic defect. Synthetic regressions
verify positive-row accounting, late grounded evidence, contradictory overflow,
partial malformed data, source grounding, and explicit uncertainty; they do not
establish temporal detection by a real model. Another live replay requires a new
bounded operator reservation and manual semantic adjudication.

## Exact approved sources

The `approved-source-v3` contract preserves the signed answer as an immutable
source record: `id`, `start`, `end`, and exact `text`. IDs derive from the source
text hash and its half-open character span, never from a model paraphrase. The
current conservative unit is the **whole answer**, not an inferred semantic
clause. Punctuation, compounds, inline code and examples are not split into
possibly misleading fragments. Offsets count Python string characters. Decision
questions remain verbatim context; an answer such as “Yes” still requires that
context to interpret it.

Source units, semantic obligations and code checks are different things. A whole
answer can express many obligations, and multiple first-stage code checks can
refer to the same source ID. The first response uses `schema: source-checks-v1`,
keeps its code checks in `requirements`, and separately declares
`source_coverage: [{source_id, scope: "all_obligations"}]`. Every code check must
bind a known source. It must not supply a `needs` paraphrase; if `needs` or
`source_text` is supplied, it must equal the entire source exactly. Runtime
supplies the displayed source text and retains the exact catalog once in
`approved_sources`. A clipped display label always points to that full catalog.

The critic is numbered independently from the first reader's code checks. It
examines each original source once, with all its obligations and bounded code
hints, rather than using the reader's labels as its scope. Every coverage row
and finding must echo the expected source ID. Coverage rows also explicitly
attest `scope: all_obligations`; selected-check coverage is incomplete. The
existing three-trace guidance applies **per semantic requirement within the
source**, not three traces for an entire multi-obligation paragraph. A specific
unresolved clause remains inconclusive. This accounting is an explicit model
attestation, not independent proof that it performed each reasoning step.

Missing or unknown IDs, narrower text echoes, missing whole-source attestations,
or mismatched critic IDs cannot support `follows`. A completed critic cannot
repair an invalid first-stage binding into a completed first reading. Grounded
partial witnesses remain advisory evidence; neither a witness nor a positive
judgment may replace the original source's scope. Uncertainty propagates across
the code checks sharing that source, while evidence is retained once.

The existing 6,000-character model-response limit and four-finding critic limit
remain. A separate input guard, `REVIEW_CONTEXT_MAX_CHARS`, permits at most 6,000
characters in the fully assembled approved context. This is a new conservative
ceiling to bound authority text admitted alongside the 20,000-character diff,
not the output-JSON limit or a token conversion. The two limits happen to share
a value and can be adjusted independently. Empty authority or oversized context
returns explicit incomplete without inference; full source and context metadata
are still retained in the saved reading, without clipping. Shared decision questions and answers retain every original
character and every sibling; they are never silently stripped, clipped, or
omitted to fit the budget. The saved `approved_context` records the original
question and those exact sibling texts. No token allowance,
provider effort, timeout or transport retry changes accompany this contract.
Saved readings and proof exports retain source IDs, the exact catalog, and
bounded controlled binding diagnostics. Read-only access starts no inference,
and earlier reports and proofs are not rewritten. The new contract version
prevents an explicit finish from reusing an older source-unbound completed review.

### What source traceability does and does not establish

The captured duplicate-handling failure changed the approved “ignore missing
keys and later duplicates” into “Ignore later duplicates of already-removed
keys.” The old critic checked that narrower label and completed with no finding.
Offline regression rejects that source substitution and verifies that the
critic receives the complete original text.

Non-temporal regressions cover source narrowing from a nonempty-string rule
that also applies when an option is missing, and conflating absent mapping keys
with explicit `None`. The former campaign's saved requirement already retained
the rule but incorrectly marked it honored. The latter campaign had a failed
held-out outcome but no saved normalized review; its narrowed-response fixture
is synthetic. These are different evidence claims.

A faithful source ID can still accompany a false `honored` judgment. An explicit
negative-control test preserves that limitation: correct IDs and source text do
not execute code or verify type checks, temporal traces, defaults, or presence
semantics. No real-model semantic improvement is established by these tests.
Because a whole-answer label may include unrelated obligations, future observers
must not classify temporal acceptance by a regex over that shared label. New
bounded source diagnostics and behavioral-witness candidates support manual
adjudication; another paid experiment requires a fresh operator reservation.
