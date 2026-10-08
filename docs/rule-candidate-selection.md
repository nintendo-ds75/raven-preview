# Standing grants during answer selection

An unsigned retry can contain the same answer as an earlier signed standing
grant. Its exact question match and newer timestamp must not hide the grant's
authority when that grant is relevant and currently eligible.

The ladder retains its existing eight-candidate answer arbitration. A separate
standing-grant budget additionally preserves a relevant grant for the
byte-identical selected answer. Grant membership and existing memory/repository/
namespace filters apply before both candidate limits: 32 FTS matches plus 32
recent grants, deduplicated by ID. At most 64 rows are scored, with at most eight
returned, ordered by similarity, timestamp, then ID. Unsigned retries cannot
consume this reserved budget. Membership only nominates a candidate. The ladder
checks declared conditions using current
repository and explicit facts, structured applicability, grant scope, and the
existing source-review and observation-refresh guards. Historical facts only
compare scope; they never fill missing current facts. Differing answers retain
their existing arbitration, and retrospective questions retain their history.

The check runs before and after semantic selection, including the wider search
and background publication path. A replacement uses its own relevance score
and still passes contrast, entailment, composition and conflict checks. Selector
presentation distinguishes actual human signoff from origin, inherited rule
authorization, declared grant terms, current applicability and current grant
eligibility. A `memory_grant_selected` event records the two candidate IDs and
the reason; neither historical row is rewritten by selection.

This is bounded retrieval, not an exhaustive grant search. Other grants can
fill the finite FTS/recent budgets or the eight returned slots; a grant below
the relevance floor can still be absent. Without FTS, only the 32 recent grants
are considered. These limits concern competing grant rows, not unsigned retry
pressure. A grant does not
automatically win a substantive disagreement, authorize a different answer,
infer business facts, or establish missing source permissions. Automatic rules
and the existing required-approver checks still govern publication. No identity,
signature, authority-map, connector or owner-link behavior changes here.

Provider-free controls are in `tests/test_rule_candidates.py`. Its DB-backed
`RuleCandidateSelectionTests` exercises deterministic, semantic, wide-search
and background publication behavior, retained history, bounded retry pressure
and rejection controls. `SelectorGrantPresentationTests` is pure with mocked
providers and opaque source-guard outcomes. Existing namespace retrieval tests
remain in `tests/test_context_memory.py`; these tests do not certify live
provider behavior or reproduce a private exported graph.

`ReservedGrantCandidateTests` in `tests/test_reserved_grant_candidates.py` adds
DB-backed controls with 33 and 100 unsigned retries, finite query/scoring/display
budgets, stable ties, FTS and no-FTS paths, repository/namespace separation, and
ineligible, unrelated and conflicting grants. In the synthetic 100-retry
measurement, reserved nomination made three SQL queries and scored one grant;
the selector received eight memories in a 4,238-character prompt. Prompt bytes
depend on the actual bounded candidate fields and these figures are not model
quality or latency claims.
