# Read-only objective research

`bridge.objective_research.research` investigates facts before the objective head
asks for a consequential judgment. It is **not another execution harness**.

The configured Raven model chooses up to six actual tool steps:

- `search`: lexical retrieval across the full eligible repository source corpus,
  using the same source/namespace/freshness boundary as existing Raven retrieval.
- `list`: paginate source metadata, twelve records at a time.
- `read`: read one complete current source version through `Store.get_record`.
- `finish`: report cited observations and a factual answer, no material move,
  genuine remaining judgment, or an inconclusive result.

The model cannot refresh providers, import records, widen permissions, run code,
join meetings, send messages or sign anything. Source content is untrusted. A
search result is not treated as a full reading. Findings require exact contiguous
quotes from fully read current versions. Interpretations and missing permission
cannot be relabeled a factual answer.

The bounded reading budget is 96,000 characters. Whole records that do not fit
are refused instead of silently excerpted. Up to three initial head sources can
seed complete readings; the remaining budget allows targeted older evidence.
A source/version digest covers the entire eligible local corpus, and is checked
before and after every model turn and again on return. The caller must recheck
it under its own publication fence before persisting a proposal.

Coverage reports eligible/read/unread source counts, characters, rounds, searches
and limits. For read meeting chunks it also reports imported completeness,
active/read chunk counts and whether the complete meeting was actually read.
A fact found in an early chunk can resolve a question without claiming the rest
of a long meeting was inspected. Lexical ranking is not proof of relevance or
completeness; the output explicitly retains that limitation.

The head should return a grounded factual answer or no-op without making a new
judgment when `disposition` is `factual_answer` or `no_material_move`.
`inconclusive` is not evidence that a human must perform the research; it means
this bounded pass did not establish an answer. Only `needs_judgment` proceeds to
the ordinary source-grounded proposal and existing core approval workflow.
All model observations remain advisory: exact quotations establish provenance,
not the correctness of every interpretation.

Verification uses real SQLite, versioned source records, namespace/availability
checks and lexical retrieval with **scripted inference**. Tests include an actual
270k-character meeting with more than twelve chunks, retrieving its earlier
relevant fact, source revocation/new evidence during a model turn, invented and
unread citations, scope-crossing reads, exhausted budgets and unavailable tools.
No authenticated live inference was performed by these tests.

## Metadata disclosure dependencies

Catalog titles/references and search/list results can influence model-written
findings even when the corresponding bodies were not read. The research result
therefore returns `metadata_dependencies`, exact record/version pairs for every
source whose metadata was actually shown to the model. These are **not** full
reads or supporting citations. `coverage.metadata_sources` and
`metadata_only_sources` distinguish them from body coverage.

Downstream proposals, objective research events and cached findings must retain
these pairs as disclosure dependencies, alongside body-read dependencies. Later
revocation of an unread title's source must withhold copied findings that may
paraphrase that title. The `revalidate` helper checks both sets. No consumer may
infer that uncited or unread metadata was safe to forget merely because the
final observation quoted a different source.

Each metadata dependency also carries `metadata_sha256`, produced by
`metadata_fingerprint(source_or_summary)`. This binds the exact canonical
`record_id`, `title`, `kind` and `ref` shown in catalog/search/list projections;
whitespace and spelling are retained. It intentionally excludes the source
version and body. A body-only revision of a metadata-only source need not stale
an unrelated accepted scope when these shown fields remain identical.

The observed `source_version_id` remains available for audit. Ongoing research
and publication still enforce their strict corpus/version fence. Persisted
metadata-only dependency checks must first verify current source access, then
compare the semantic metadata hash. Equal hashes never restore revoked access,
and body-read/supporting dependencies retain their ordinary version semantics.


## Persistent background request budget

Automatic objective and repository-watch investigations share a persisted budget
of **six attempts per repository per rolling hour**, with at least **60 seconds
between starts**. Transcript revisions, new objectives, model changes, restarts,
and explicit watch toggles do not reset this budget. Claims are made under the
existing SQLite writer/PostgreSQL advisory transaction lock immediately before
an evaluation starts; cached, pending, capped and throttled requests do not
spend an attempt. An inference attempt that fails or loses its process still
counts. Every attempt is separately bounded to six research rounds (requesting 2,200 output
tokens each), 96,000 read characters, and at most one proposal call requesting
3,000 output tokens. These are logical-call and requested-output budgets, not
a dollar-price estimate or a claim that model input equals full source length.
The existing core model transport may retry transient HTTP failures up to four
times and Anthropic thinking discovery can add a bounded extra call/thinking
allowance. The existing Claude CLI path has a wall-time bound, not an enforced
output-token limit. Provider-side spending limits remain necessary.

`background_budget(store, repo)` reports remaining attempts, rolling-window
size, `available`/`throttled`, and `next_retry_at`. A throttled scan returns
`status:throttled` with this metadata; it is not completed research, an empty
result, or a new judgment request. Watches retain their dirty corpus until a
real evaluation can run. An explicit watch configuration update clears its
handled-corpus pointer to permit recovery, but never clears budget history.

A deliberate authenticated manual scan is separate user-requested inference
and does not spend or reset the automatic budget. All current membership,
source access, scope and publication fences still apply. Automatic workers
always call `scan(..., automatic=True)` and never use the explicit failed-input
retry override. The independent three-attempt limit for identical failed
inputs remains in force in addition to the shared rolling-hour limit.

Automatic investigation now rejects the internal `claude-cli` backend because it does not enforce output-token limits. Explicit manual investigation may still use that existing backend under its timeout; this distinction does not affect the separate Claude SDK execution harness.
