# Raven documentation

Every document under `docs/` is listed here, one line each, so nothing is
reachable only by chance (`tests/test_docs_reachable.py` checks that).

**Start here**

- [What Raven is](positioning.md): the one-sentence description, who it is for, what breaks today, what Raven is not, and the boundaries each document backs.
- [Getting started](getting-started.md): install, create the workspace, connect your coding agent, run one task, troubleshoot.
- [Help](help.md): the page the app's help icon opens: setup, tasks, answering and signing, and the rules the app no longer repeats on screen.
- [How Raven works](overview.md): what a task looks like inside Raven, how answers are reused, who gets asked, where your data goes, what this preview promises.
- [Technical reference](reference.md): every MCP tool, HTTP endpoint, setting and page.
- [What Raven saved](reference.md#what-raven-saved): the proposal outcome, time-to-answer, reuse, hand-on and re-ask numbers, and where each is read from.
- [Identity](identity.md): one table per way a person reaches Raven, how they link, and which field each authorization check trusts.
- [Demo script](demo-script.md): an eight-minute walkthrough of installing Raven and running one task through the inbox, with two people on screen.

**Use Raven**

- [Work directly in Raven](browser-tasks.md): run a coding harness inside Raven from the browser, with tests in a no-network sandbox.
- [Today: persistent objectives](startup-os.md) (preview): Raven investigates imported evidence, proposes one bounded piece of work with its citations, and a person reviews and signs it before any coding worker runs.
- [Read-only objective research](objective-research.md): the bounded search/list/read loop behind a proposal, its reading budget, coverage report and the background request budget.
- [Meeting adapters](meeting-adapters.md): transcript import, the Vexa participant adapter, per-occurrence consent, revocation and the consented background observer.
- [Voice interviews](voice-interviews.md): spoken or typed answers from a decision.
- [Workspace accounts](user-onboarding.md): the two-step workspace creation, invitations, roles.

**Connect an agent**

- [Host adapters](host-adapters.md): the hooks that register a task before the model starts, hold a stop while a person answers, and resume an idle session.
- [Investigate, ask and continue](investigation-and-events.md): Raven's review of the agent's investigation and the signed wake-up events for cloud chats.
- [Execution harnesses](execution-harnesses.md): which coding harnesses Raven can run on the server (Claude Agent SDK, Pi, OpenCode, the qualified experimental Codex adapter), how each is configured and what was verified.
- [OpenCode harness](opencode-harness.md): setup and guardrails for the OpenCode adapter.
- [Independent artifact verification](artifact-verification.md): operator-held check profiles run in the sandbox against the candidate's exact bytes, and what a receipt does and does not prove.

**Integrations**

- [Slack](slack.md): notification-only by default, an opt-in conversational bot, the manifest and scopes.
- [Employee-host notifications](host-notifications.md): send the Slack notification through the developer's own Slack MCP instead of a bot.
- [Connect GitHub](github-connection.md): the shared GitHub App, or your own.
- [Context sources](context-sources.md): opt-in Slack channels, Jira projects and documentation sites, the onboarding report, and suggested sources.
- [Connected context](context-connectors.md): the optional Airweave connection.
- [Teams](teams.md): outbound webhook, and an opt-in verified channel bot.

**Operate**

- [Run one useful task together](guided-pilot.md): the guided pilot for one team.
- [End to end on an open-source repository](local-e2e-open-source.md): how to run the whole loop yourself on a real repository.
- [Slack contract testing without a workspace](slack-contract-testing.md): exercising the Slack transport against a simulated Web API.
- [Chronological coordination protocol](chronological-coordination-evaluation.md): the acceptance protocol for stakeholder discovery, focused questions, static context and safe reuse, recorded in order.
- At the repository root: [PILOT.md](../PILOT.md), [DOCKER.md](../DOCKER.md), [SCALING.md](../SCALING.md).

**Design notes and contracts**

- [Versioned context memory](context-memory.md): how records enter Raven and keep their identity and versions.
- [Record import contract](record-import-contract.md): the identity and version rules `POST /api/records` and `bridge_import_record` share.
- [Decision source revisions](decision-source-revisions.md): what happens to an answer when the source it leaned on changes.
- [Fact corrections](fact-corrections.md): replacing an unsigned node's facts explicitly, with audit.
- [Historical answers and task namespaces](cache-context-classification.md): when a prior answer is evidence and when it is only context.
- [Standing rules before contact](rule-before-contact.md): a qualified local rule can resolve a request before anyone is contacted.
- [Standing grants during answer selection](rule-candidate-selection.md): a signed standing grant keeps its authority beside an unsigned retry with the same answer.
- [Contact learning](contact-learning-contract.md): how answers and referrals teach scoped first contacts.
- [Typed context for contact suggestions](contact-context-contract.md): the evidence a contact suggestion is allowed to cite.
- [Change proof](change-proof.md): the portable finish bundle and its trust limits.
- [Bounded advisory review](advisory-review-bounds.md): what the model review of a submitted diff can and cannot say.
- [Quality rubric](quality-rubric.md): what "better" means for Raven, scored per dimension with the evidence that moves a score.

**Verification records**

- [What we have tested](verification.md): the index of end-to-end runs, with their failures.
- [Startup OS acceptance ledger](startup-os-acceptance.md): coverage per design case for Today, meetings, harnesses and verification; a ledger of historical checkpoints, not a live pass.
- [11 October customer loop](customer-loop-2026-10-11.md): fresh install, real Claude host, native connectors against synthetic services.
- [5 October customer loop](customer-loop-2026-10-05.md): the first new-customer loop without a prepared ownership map.
- [Live source revalidation](live-source-loop-2026-10-06.md): the source-change and approval loop with real Claude Code processes on urllib3.
- [Context and host validation](context-host-validation-2026-10-06.md): the context-layer and host mechanisms added on 6 October, and what was and was not validated.
- [Overnight host verification](overnight-host-verification-2026-10-06.md): 39 cold-start coding-host attempts across 21 repositories; the target of about 100 was not reached.
- [Acceptance and limits](acceptance-2026-10-06.md): a matrix separating implementation, protocol tests, runtime tests and external-service verification.
- [Verification on 6 October](verification-2026-10-06.md): execution evidence for the reconstructed preview, including Docker, PostgreSQL and Chromium.
- [Restored review fixes](reconstruction-2026-10-05.md): the cloud review's changes reconstructed from the saved conversation.
- [Retained-host review acceptance](raven-review-acceptance.md): the gated live run of the advisory review in its original authorized runtime.

**Plans**

- [Make Raven reliable to connect, understand and evaluate](engineering-and-evaluation-plan.md): the engineering and evaluation plan.
- [Workbench build plan](workbench-build-plan.md): the plan for browser execution.
