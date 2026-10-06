# Workflow acceptance and remaining limits

This matrix distinguishes implementation, deterministic protocol tests, actual
runtime tests and external-service verification. Synthetic people never establish
real business authority. Public repository contributors used for discovery are
not contacted or represented as having approved the test changes.

## Core workflow

| ID | Requirement | Implementation and verification | Remaining boundary |
|---|---|---|---|
| A01 | Fresh install | `setup`, Docker/PostgreSQL and native loopback path; `test_setup*`, `test_fresh_native` | Re-run the latest image after packaging changes; container results apply only to their recorded checkpoint |
| A02 | Immediate task visibility | MCP kickoff, original goal/requester, idempotent key; `test_canvas`, HTTP contract harness | Host must actually call Raven; installing an MCP server alone cannot enforce that |
| A03 | Decision-tree discovery | Static and background model discovery, parent/child nodes, required follow-ups | Model discovery is not guaranteed complete; no fixed target decision count |
| A04 | Grounded proposals | Record/decision citations, revision and applicability gates; `test_contract`, `test_applicability` | Retrieved text remains evidence until human approval; live inference quality is separate |
| A05 | Ownership and referrals | Git/source/directory signals, scoped learned contacts, clarification for missing facts | Inferred first contact is not independently verified organizational authority |
| A06 | Internal test nuance | Attributed human exclusion tested through billing protocol | Billing scenario uses synthetic owners and trusted synthetic metering events |
| A07 | Demo policy decisions | Five explicit synthetic policy decisions with multiple signers, including scoped legal hold | No real customer contract or legal approval is claimed |
| A08 | Behavioral incorporation | `test_billing_contract`: 0/499/500/501, seats, UTC months, test traffic, continuing usage, scoped hold | Pooling, rollover and hold semantics are explicitly chosen for the test, not inferred business rules |
| A09 | Authorization gate | Every required signer/follow-up; latest human changes must be read; stale signatures rejected | Governs Raven finish, not GitHub merge or production deployment |
| A10 | Durable conversation | Outbox/ingress/reply queues, retry delays, restart/readback/replay tests | Remote sends can be at-least-once after an uncertain network outcome |
| A11 | Cross-host historical lookup | MCP search/get record across fresh processes; no notification side effect | Historical approval does not grant new-use permission |
| A12 | Standing-rule reuse | Explicit opt-in, scope/conditions/expiry/revocation checks | Ordinary reuse still requires fresh review |
| A13 | Permission boundaries | Agent/viewer restrictions, live authority/team checks, supersession guard, pinned Slack workspace | One workspace per database; no per-repository multi-tenant ACL claim |
| A14 | Task-link boundaries | Person/task/decision scope, expiry/revocation and live authority; `test_brief`, `test_interview` | Forwarding a bearer link transfers its scoped ability to act |
| A15 | Voice interview | Browser dictation/readback, adaptive provider path, guided fallback, explicit confirmation | Real microphone/audio/provider execution requires separate verification; no native huddle or phone service |
| A16 | Code review proof | Exact diff SHA-256, decision revisions, attribution, citations, stale detection, offline verifier | Digest is integrity, not a human cryptographic signature; supplied checks remain host claims |
| A17 | Slack integration | Actual HTTP Web API/callback/MCP contract harness with documented bodies, HMAC and fault cases | Synthetic Slack installation; does not certify a live workspace's granted scopes |
| A18 | Source integration | GitHub/local git ingestion and host-supplied Jira/document/Slack record API | No native Jira polling, private Slack search OAuth or Slack archive backfill |
| A19 | Teams replies | Separate opt-in authenticated channel-bot adapter; signed JWT fixture tests | Live Microsoft registration/install/delivery unverified; webhook-only mode remains outbound-only |
| A20 | Operations and UI | Explicit missing-provider status, backup/restore, restart, browser suites and cancellation cases | Each test report states exact checkpoint; automated browser speech uses stubs |
| A21 | Documentation | Setup/accountless flow, backend capabilities, protocol count/scopes and trust limits | Do not convert self-hosting into an unverified no-egress or enterprise compliance claim |

## Additional adoption requirements

These are generic acceptance criteria, not copied customer conversations.

| ID | Requirement | Current disposition |
|---|---|---|
| R01 | Rough task becomes a decision tree; experts may fix the question | `reframe.py` archives old content/signatures, reroutes, clears authority and invalidates mixed dependency chains |
| R02 | Cross-functional ownership learns from sources and referrals | Source signals plus scoped answer/referral learning; live membership/expiry prevents stale verified routing |
| R03 | No mandatory manual owner map | Directory discovery and triage; manual authority rules are optional |
| R04 | Optional initial ingestion | `--repo`, GitHub sync and host imports are opt-in; cold start remains possible with triage |
| R05 | Prepared short, clearly AI-originated asks | Slack messages identify Raven as AI, carry one question, context/options and referral path |
| R06 | Slack-first with optional spoken discussion | Accountless task-link browser interview; explicit consent and exact reviewed answer |
| R07 | Delays, absence and multiple stakeholders | Durable queues/readback; required signers; temporary cover is not permanently learned; one alternate coordination escalation preserves authority |
| R08 | Load-aware routing | Future direction; no claim to measured workload optimization or sufficiently-capable load balancing |
| R09 | Reusable judgments across hosts | Shared durable decision store with structured MCP, Slack messages and human overview |
| R10 | Provenance, decay and contradictions | Owner/time/reason/source revisions, recency-weighted retrieval, scoped reuse and authorized supersession |
| R11 | Speculative independent work without false finality | Proposed/evidence states remain unapproved; finish rejects unresolved approval |
| R12 | Human correction, override and rework | Authenticated scope authority, explicit admin override, reassignment, answer correction and question reframing |
| R13 | Whole-run visibility | Task tree, contact/delivery trace, notes, model status, blockers and saved proof |
| R14 | Ordinary coding workflow over MCP | Thirteen tools expose task/source/history/wait/finish/proof; human replies need no portal |
| R15 | Specification to code and normal review | Real public-codebase change tested independently and bound to a Raven proof; publication is separately authorized |
| R16 | Stop at review boundary | No implicit merge, infrastructure provisioning or production deployment authority |
| R17 | Single-user adoption | Experts answer in normal Slack threads or their narrowly scoped task link |
| R18 | Self-hosted deployment | Docker/PostgreSQL and native Python paths; enterprise hosting controls remain operator responsibilities |
| R19 | Honest data handling | Customer-configured providers receive relevant context; no no-training, residency or zero-retention guarantee is inferred |
| R20 | Real evaluation rather than demo-only success | Separate real OSS implementation/native tests, billing behavioral tests and faithful transport faults; inference semantics must be evaluated independently |

## Privacy and data retention

Raven stores decision context, source records, identities, audit events and saved
interview text in its configured database. Canceling an interview is not deletion.
Raw microphone audio is not stored by Raven, but the browser's speech-recognition
service may process it externally. Adaptive interviews and other inference send
their needed context to the configured provider. Slack live-search text and
action tokens are transient and are tested for non-retention.

There is no automatic application-wide retention/deletion policy or verified
enterprise residency/no-training contract. Backups and exports also contain
workspace data and need operator access controls and retention rules. Ordinary
container shutdown preserves data; destructive volume removal is a separate
operator action. Never deploy unrelated tenants in one shared database and rely
on the repository field as an access boundary.

## Evidence is not a scripted product result

`evals/billing_contract.py` is a separate reference implementation with explicit
synthetic decisions. Production routing and approval code contains no special
case for its example customers or numeric policy. The HTTP Slack harness scripts
provider completions to isolate transport and authorization; it does not prove
that a live model will discover or interpret every question correctly. A fresh
public-codebase implementation and its native tests are a separate check, not a
substitute for representative multi-team pilots.
