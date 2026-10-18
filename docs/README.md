# Raven documentation

**Start here**

- [Getting started](getting-started.md): install, create the workspace, connect your coding agent, run one task, troubleshoot.
- [How Raven works](overview.md): what a task looks like inside Raven, how answers are reused, who gets asked, where your data goes, what this preview promises.
- [Technical reference](reference.md): every MCP tool, HTTP endpoint, setting and page.
- [What Raven saved](reference.md#what-raven-saved): the proposal outcome, time-to-answer, reuse, hand-on and re-ask numbers, and where each is read from.
- [Identity](identity.md): one table per way a person reaches Raven, how they link, and which field each authorization check trusts.

**Connect an agent**

- [Host adapters](host-adapters.md): the hooks that register a task before the model starts, hold a stop while a person answers, and resume an idle session.
- [Investigate, ask and continue](investigation-and-events.md): Raven's review of the agent's investigation and the signed wake-up events for cloud chats.
- [Work directly in Raven](browser-tasks.md): run Claude or Codex inside Raven from the browser, with tests in a no-network sandbox.

**Integrations**

- [Slack](slack.md): notification-only by default, an opt-in conversational bot, the manifest and scopes.
- [Employee-host notifications](host-notifications.md): send the Slack notification through the developer's own Slack MCP instead of a bot.
- [Connect GitHub](github-connection.md): the shared GitHub App, or your own.
- [Context sources](context-sources.md): opt-in Slack channels, Jira projects and documentation sites, the onboarding report, and suggested sources.
- [Connected context](context-connectors.md): the optional Airweave connection.
- [Teams](teams.md): outbound webhook, and an opt-in verified channel bot.
- [Workspace accounts](user-onboarding.md): the two-step workspace creation, invitations, roles.
- [Voice interviews](voice-interviews.md): spoken or typed answers from a decision.

**Contracts and behaviour**

- [Versioned context memory](context-memory.md) and the [record import contract](record-import-contract.md): how records enter Raven and keep their identity and versions.
- [Decision source revisions](decision-source-revisions.md), [fact corrections](fact-corrections.md), [historical answers and task namespaces](cache-context-classification.md).
- [Standing rules before contact](rule-before-contact.md) and [during answer selection](rule-candidate-selection.md).
- [Contact learning](contact-learning-contract.md) and [typed context for contact suggestions](contact-context-contract.md).
- [Change proof](change-proof.md) and [bounded advisory review](advisory-review-bounds.md): what the review of a submitted diff can and cannot say.

**Evidence**

- [What we have tested](verification.md): the index of end-to-end runs, with their failures.
- Run reports: [11 October customer loop](customer-loop-2026-10-11.md), [5 October customer loop](customer-loop-2026-10-05.md), [live source revalidation](live-source-loop-2026-10-06.md), [context and host validation](context-host-validation-2026-10-06.md), [overnight host verification](overnight-host-verification-2026-10-06.md), [acceptance and limits](acceptance-2026-10-06.md), [verification on 6 October](verification-2026-10-06.md), [restored review fixes](reconstruction-2026-10-05.md), [retained-host review acceptance](raven-review-acceptance.md).
- How to run one yourself: [end to end on an open-source repository](local-e2e-open-source.md), [Slack contract testing without a workspace](slack-contract-testing.md), [chronological coordination protocol](chronological-coordination-evaluation.md).

**Plans**

- [Make Raven reliable to connect, understand and evaluate](engineering-and-evaluation-plan.md) and the [workbench build plan](workbench-build-plan.md).
- [Run one useful task together](guided-pilot.md) and, at the repository root, [PILOT.md](../PILOT.md), [DOCKER.md](../DOCKER.md), [SCALING.md](../SCALING.md).
