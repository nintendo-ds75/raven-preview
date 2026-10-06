> **Experimental archive: known validation failures; not merge-ready or deployable.**
> See the [validation status](source-contact-claims-validation.md). Use `main` for current validated code.

# Experimental source-cited contact discovery

This candidate is opt-in with `BRIDGE_CONTACT_CLAIMS=1`. It is limited to the
canvas node workflow and Slack contact invitations. Keep it disabled until its
semantic-reader accuracy, PostgreSQL migrations, and runtime UX have been
independently validated. No real-provider accuracy is established by the offline
contract tests.

## What changes

Imported source records can describe a primary contact, a backup, or a
coordinator. Those roles are different from authorship and from signer authority.
`contact_claims` reads bounded path-linked source records and suggests contacts.
It does not call an assignment or approval API and cannot write authority rows.

When the mode is enabled, weak inferred contacts (including plain authorship
fallback when extraction is unavailable) receive contact-only invitations. Their
person IDs are stored in `contact_person_id`; `owner_id` remains empty. The node
API exposes a `contact_candidate` with citations and an explicit next step.
Receipt, acknowledgment, an answer, or imported text does not assign anyone.

The bound, authenticated Slack recipient can reply `claim` or `ask @person`.
Raven states who would own this particular question and the resulting ability to
answer/sign it. Only a second explicit `confirm contact` assigns that question.
The existing `claim_slack_question` authorization and conditional assignment are
reused. A route-learning opt-out prevents the confirmed task-only contact from
becoming standing ownership when they later answer. Answer/signoff remains a
separate human action under existing authorization checks.

Verified authority maps, structured CODEOWNERS/maintainer contacts, explicit
assignees, scoped learned human routes, and configured verified-only routing
retain precedence and their existing behavior. Disabling this candidate retains
the legacy routing behavior, including its existing inferred-owner semantics.
Teams and legacy direct request APIs are not reinterpreted by this candidate.

## Source-owned evidence contract

A reader receives at most 12 applicable sources, each with at most 6,000 body
characters, and at most 100 existing active member/admin identities. It sees the
repository, paths, question, context, category, and facts. A too-large or
incomplete evidence set abstains; source text is not clipped through a possible
qualification. The source supplies whole-paragraph spans with exact offsets.

A claim includes:

- A source ID and one of that source's complete evidence spans
- An existing person ID and exact full name, email, or Slack ID in the source
- Role: primary, backup, or coordinator
- Confidence, polarity, and modality
- A source phrase identifying the relevant topic and source-owned paths
- Explicit conditions and an ISO expiry when actually present

The validator rejects unknown/ambiguous identities, unsupported source/span/path
references, scope expansion, weak or negated claims, proposed or conditional
assignments, contradictory primaries, qualified evidence, expired/old timestamps,
and selected instruction-like source text. It requires at least two meaningful
shared topic terms. Conditional claims currently abstain; they are not converted
into permanent coverage. Unknown identities never create accounts.

These guards are deliberately conservative. Exact citations and lexical guards
do not prove semantic correctness or defeat every prompt injection. The model's
interpretation of a factual relationship still needs separate evaluation. The
legacy record API substitutes import time when original source time is omitted;
the current 365-day record-age guard therefore cannot prove an undated source
fresh. That limitation is another reason these are contact invitations, never
verified ownership.

## Caching and ordering

Cache identity includes the entire bounded source content, paths, status,
locator, record timestamp, active directory identities, repository, question,
context, facts, category, and contract version. Source/person changes during the
reading discard it. Expiry is rechecked on every use. Malformed, conflicting,
failed, and uncertain results are not cached as successful claims.

An unambiguous applicable primary can precede otherwise weak inferred contacts.
Map/listing and scoped learned human routes keep precedence. Existing declines,
requester exclusions, and unavailability still apply. Backups/coordinators remain
fallback candidates; a missing primary never invents one. Conflicting claims do
not use author recency as a proxy for accountability.

## First-notification lifecycle

The existing initial `model_pending` phase withholds notifications. Extraction
runs inside that background phase, outside database write transactions. Its result
is converted into a contact-only assignment in the same revision-checked write
as the rest of the model reading. The invitation is enqueued afterward.

The deterministic first pass also removes weak owner standing before publishing
the node. If inference fails, the ordinary contact hint is used as an explicitly
unverified invitation. An idempotent node retry does not recontact people because
source text changed. A newer human action defeats the old background result.
Queued invitations are checked again for current recipient/activity/assignment
before sending. Claim confirmation checks the sent notification, bound recipient,
question/revision, target activity, task state, and pending confirmation atomically.

No authentication/authorization rules were weakened. Contact invitations omit
signing/login links. Source quotations in messages are escaped to prevent Slack
mention syntax from becoming additional outreach.

## Verification boundaries

`tests/test_contact_claims.py` exercises the schema, cache invalidation, ordering,
source scoping, abstention, source-instruction handling, and contact invitation
lifecycle using scripted reader outputs and a local fake Slack transport. Its
classes are registered in the PostgreSQL suite. Scripted outputs test the
contract; they do not measure real natural-language extraction quality.

Before enabling outside isolated tests:

1. Independently review the authorization and concurrency boundary.
2. Run actual PostgreSQL and browser/Slack-UX validation on the frozen revision.
3. Reserve a separate, bounded provider check against representative primary,
   backup, ambiguous, conflicting, negated, stale, scoped, and adversarial text.
4. Report abstention and wrong-contact rates separately from approval integrity.

There is no claim that this candidate fixes the two original host cases under a
real provider until that reserved end-to-end check is complete.
