# Chronological coordination acceptance protocol

This protocol evaluates stakeholder discovery, focused questions, useful static context, and safe reuse of previous interactions. Code correctness is a separate optional outcome. Use the existing [audit engine](../evals/audit/README.md) for freezing, rubric pinning, capture, scoring and visualization, with the [strict chronological supplements](../evals/chronological/README.md). The public utilities contain no private case gold and make no live-host acceptance claim.

## Case and source identity

Freeze an exact Raven commit/tree, host version/profile, evaluator manifest, repository commit/tree, exclusive cutoff time, context admission receipts and oracle version before execution. Preserve failed attempts and later adjudications separately. A recovery is not a new independent first-attempt success.

Choose a real user need observable at the cutoff that permits meaningful design or business choices. Keep the later reference change and author evaluator-private. Build stakeholder eligibility from pre-cutoff ownership, scoped contributions and admissible reviews. Multiple people may be defensible. A future patch author is not an eligibility rule, and contribution counts are not approval authority. If evaluators saw future metadata while selecting the case, disclose that limitation instead of claiming human blinding.

The first ordinary user prompt must not name Raven, prescribe its tools or supply the answer. Runtime machinery may be current; project evidence must obey the cutoff. Historical domain time, physical execution time and synthetic stakeholder-event time must be declared separately. Do not silently retimestamp sources, disable expiry, or infer that all old people remain available.

Every stakeholder response in a synthetic fixture is labeled as a test double. Do not contact actual project maintainers. Context prose can suggest whom to ask, but cannot grant approval. Approval must come from the explicitly modeled authenticated actor and the supported current confirmation path.

## Acceptance matrix

Each row produces its own observed result, evidence references, profile and limitation. No aggregate percentage substitutes for a failed authority or source-isolation gate.

| ID | Observable acceptance criterion | Required evidence | How it can be exercised |
|---|---|---|---|
| H01 | Later code and context are unavailable at test time | Physical object/ref audit; denied evaluator-file reads under actual host identity; enumerated tools/routes; admitted and rejected context receipts | Offline export is implemented. Actual UID/tool/network admission remains mandatory before a live host. |
| H02 | Fresh setup is actually discovered and used | New home/config; actual tool discovery; ordinary prompt triggers supported hook; receipt and exactly one task | Real official host required. A config file or simulated process cannot pass this row. |
| H03 | Capability status is accurate | Configured, reachable, tools discovered, hook loaded, prompt registered, evidence admitted and continuation verified are distinct fields | Record actual checks. An earlier state does not imply a later one. |
| H04 | Cold first contact is defensible | First successful notification recipient, original question/scope and pre-cutoff eligible-person evidence | Real model decisions with labeled synthetic replies are valid; transport realism is reported separately. |
| H05 | Coordination authorship is distinguished from accountable ownership | A context fixture names a coordinator separately; score first route, justified referral and final signer independently | Scoped adversarial contact fixture. A recovered route does not erase an unnecessary first interruption. |
| H06 | Questions are relevant, clear, focused and necessary | Original question arguments, returned context available at that instant, unresolved choice, interruptions and evidence-linked rubric | Manual review required for important conclusions; model-judge unavailability is not a pass. |
| H07 | Static facts are useful without needless approval | Exact current scoped source citation; no unnecessary ping; no new authorization implied | A factual lookup case separate from approval controls. |
| H08 | Work items, sources and interactions form connected history | Canonical external identity/namespace, task anchors, versioned source/decision edges and external-reference lookup | Use supported import/lookup paths. Text search alone does not establish typed linkage. |
| H09 | Warm history avoids repeated questions | Fresh host context on preserved graph; correct scoped history/citations; no new notifications for a read-only request | Real model session, with earlier host files inaccessible. |
| H10 | Prior knowledge can be found without supplied IDs | Paraphrased need and current facts, realistic near-topic distractors, correct interaction or justified focused clarification | Separate live case. Exact-reference retrieval does not substitute. |
| H11 | Applicability conditions govern standing reuse | Matching positive control reaches an eligible rule; missing/changed facts reach and fail the relevant condition; exact authorization reasons | Independent graph copies. Safe misses cannot prove scope enforcement when the positive source is unreachable. |
| H12 | Source changes remove current authority and revalidation binds current pins | Before-host snapshots, refusal, displayed current-source review, signed pins, material reversion/new epoch and explicit standing regrant | Real conversation plus separately labeled protocol checks. Completed proof/history remains immutable. |
| H13 | Provider synchronization and Raven freshness are distinguished | Provider event/sync times, Raven observation/head, lease state, edit/delete/revocation/outage/restoration receipts | Simulated contracts and real provider checks are separately labeled. Missing search hits never imply deletion. |
| H14 | Connector claims reflect real output and permissions | Actual Slack/GitHub/Jira/context-provider tool responses, authorization evidence, admissible imports and visible instance state | Synthetic ACL objects cannot certify live compatibility. Missing ACLs may correctly produce an inadmissible result. |
| H15 | Follow-up, restart and new-task intent are handled correctly | Same-session/task receipts for ordinary follow-up/restart; explicit new-task request yields a new task; no duplicate retry | Actual supported host mechanism. Subprocess success alone is insufficient. |
| H16 | Hook failure is visible and bounded | Cumulative delayed responses produce an error before the host deadline; no duplicate start | Fault injection plus actual loader execution, with exact timing profile recorded. |
| H17 | Background continuation achieves its permitted outcome | Same host session and expected result; callback receipt; duplicate/crash-before-ack/lease/permission-denial controls | A new host context or exit code 0 cannot establish same-session continuation. |
| H18 | Requester and conversational learning stay attributable | Requester, actor, person asked, thread/task/source, referral/follow-up events and graph deltas | Supported ordinary identity fields only, within the case's authorized scope. |
| H19 | Every result has an observable audit trail | Model-owned versus evaluator calls, context, questions, replies, signatures, dependencies and invalidations; recorded graph snapshots | JSONL audit plus independently preserved digest and rendered timeline. |

## Question and routing rubric

Freeze the eligible-person set before inspecting live questions. Keep separate categories for a direct correct contact, justified discovery/referral, incorrect first route, safe inability to identify a person, and unavailable evidence. Preserve contact timing and original recipient IDs. Eventual correct signoff is useful but does not change first-recipient scoring.

Use the existing scorer's six `question_quality` keys, each with `pass`, `fail`, or `unknown`. Each scored axis requires a named judge and `evidence_events` references in the audit ledger:

- **necessary:** The question addresses a material unresolved choice and does not repeat an answer already available in the relevant scope. Unnecessary interruptions fail even if later recovered.
- **answerable:** The recipient can give a focused actionable answer. A broad bundle or a request to decide everything fails; a clear conditional choice can pass.
- **context_sufficient:** The question supplies the relevant known constraints and evidence without relying on an undisclosed assumption.
- **neutral:** The wording separates evidence, uncertainty and proposed options without falsely presupposing approval or steering toward the hidden reference solution.
- **readable:** The recipient can understand the request and its practical consequence in clear language.
- **scope_clear:** Current repository, customer/release/environment, applicability and requested authority are clear where material.

Minimality and nonredundancy are assessed through `necessary` and `answerable`; do not introduce a second numeric score format. Every score cites exact event IDs and the information available before the answer. Human-supplied policy in a later reframe cannot count as autonomous discovery. A proposed UI-only solution need not ask backend questions unless its actual design changes the backend contract. Accept behaviorally valid alternative designs.

## Cache and authority sequence

Use a genuine signed interaction as a historical supplier where possible. Synthetic distractors must never inherit its signature or task scope. Test static retrieval separately from authorization, same-task continuation separately from a new task, and evidence-only memory separately from explicit reusable grants.

For source lifecycle testing: approve source v1; change it to v2; observe the exact active dependent's authorization and review fields before host actions; verify refusal; explicitly review/sign the current source; inspect every persisted pin; then change it again. A material reversion to earlier text is a new revision, not permission to revive an old standing grant. An identical current-head import is a no-op. Material pin changes require a new explicit standing opt-in after task-specific reapproval. Legacy unversioned provenance must remain honestly unknown rather than acquiring fabricated pins.

Keep completed history and proof bytes unchanged. An active task already waiting for another issue does not establish source invalidation; inspect the particular previously authorized node. A host that safely declines completion does not itself prove the backend refused a finish call.

## Run admission and stopping

Require an exact qualified Raven source, supported host setup, proven cutoff boundary, no readable gold, explicit actor/source/currentness, frozen oracle and sufficient bounded completion budget. Each case has a clear outcome and stopping condition. Track measured provider cost, unknown final usage, conservative reserves and pending requests separately.

Stop a case on authority/isolation breach, explicit permission denial, a permanent provider/account error, a known-invalid evaluator setup or its admitted resource ceiling. A provider-wide permanent error stops subsequent launches until resolved. Preserve raw evidence, classify product/model/evaluator/infra failures separately, and avoid repeated partial continuations that cannot finish the workflow.

The release runner must honor its separately approved scope/exclusion manifest. Unadmitted or blocked categories remain unavailable even if listed in a broader product roadmap; renaming a scenario does not authorize it. Do not expand persistent access or contact outside people through the benchmark.

## Claims and limitations

The existing audit engine implements source export, rubric pinning, observable-event hashing and timeline rendering. The chronological supplement adds exclusive cutoff selection, author/committer-time checks and conservative context metadata admission. Their synthetic unit tests do not constitute real host-loader, connector, stakeholder or warm-cache acceptance. Each live report must label actual execution, simulation, offline checks, not-run stages and missing evidence.

Public later changes may be present in a model's training data. Temporal isolation prevents test-time retrieval of those changes; it does not prove absence of pretraining knowledge. A later reference patch is not an exhaustive oracle. Code tests, stakeholder correctness, question quality, evidence freshness and authorization are distinct outcomes.
