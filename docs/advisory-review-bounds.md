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

The first stage still lists each requirement. Counterexamples and extra
conditions are sparse: at most four checks, four early-exit findings, or four
conditions per respective collection. These are finding-output limits, not a
four-requirement limit. Prompts request short labels, exact short code fragments,
and one-sentence concrete findings, with no analysis transcript or explanations
of successful checks. If the scope or findings do not fit, the model must report
inconclusive rather than omit coverage silently.

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
without increasing model budgets, adding provider-specific parameters, or
removing adversarial review. Non-review completion retry behavior is unchanged.

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

A kept reading is bound to the ordered questions and answer text of the signed
decisions supplied to the reviewer, including other decisions used as context.
Reframing a question and freshly approving the same answer (such as “Yes”)
requires a new reading. Changing shared signed context marks all findings stale;
co-signing or changing only timestamps or rationale does not cause another read.

Older readings without question/input fingerprints remain visible as stale, with
an explicit missing-metadata reason. Reading the tree never starts inference.
An explicit finish refreshes them under a new input-aware cache key; historical
review events and exported evidence bundles are not rewritten.

Run deterministic checks with:

```sh
python -m unittest discover -s tests -p 'test_bounded_review.py' -v
python -m unittest discover -s tests -p 'test_review_invariants.py' -v
python -m unittest discover -s tests -p 'test_review_inputs.py' -v
python -m unittest discover -s tests -p 'test_proof.py' -v
python -m unittest discover -s tests -q
```

These tests use synthetic response bodies, sanitized truncation metadata,
executable temporal controls, and the actual parser/persistence paths. They do
not call a provider or establish real-model semantic reliability.
