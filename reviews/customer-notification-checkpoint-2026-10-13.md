# Customer interaction checkpoint, 13 October 2026

Base: `ec5817d31d7a9be9fc8bdd2562b63806c6b11216` (remote default branch
`main`, verified by fetch during this work). This is an implementation and
focused regression checkpoint, **not** a completed live customer acceptance.

## Reproduced gaps and changes

- Existing-host employee Slack delivery had no capability/receipt protocol.
  Added a principal/task-bound, durable one-shot notification claim. Recipient
  email and known Slack ID must match the exact active owner. Closed tasks,
  settled requests, stale revisions, missing permission assertions, invented
  tools and duplicate server/host delivery are rejected. Ambiguous delivery is
  never automatically retried. Receipts remain explicitly host-reported.
- Server bots previously interpreted Slack answers by default. Fresh default
  now sends only a short authenticated Raven task/decision link. It does not
  send task evidence or interpret replies. Legacy conversations require an
  explicit setting; legacy regression fixtures now declare that setting.
- Expert pages lacked read-only task clarification. Both the authenticated
  decision modal and recipient page now offer it. The API rejects cross-task
  focus and body scope/identity overrides, never searches neighboring tasks,
  and creates no decision, signature, or coding session. It rechecks visibility,
  text fingerprints and reader/link validity after inference.
- A malformed support-check response could allow an unsupported model answer.
  Empty/malformed/unknown verdicts now fail closed. Exact citations and a
  nonempty well-formed positive support check are required.
- Reduced primary navigation to conversation, inbox, tasks. The overview shows
  next action, resolved decisions and remaining questions with owners. Detailed
  evidence remains on demand and explicit signoff controls retain required
  review context. Login preserves a bounded internal focused-decision route.

## Tests actually run at this checkpoint

- Initial full Python suite, launched before these edits: **2,454 tests passed,
  four skipped**, 506.735 seconds. This is not the final changed-tree result.
- Focused current inquiry/host/default notification/security checks: **43 passed**.
- Existing inquiry/host regressions: **34 passed** before the last security additions.
- Legacy brief, Slack capture, transport, plus fresh notification-only tests:
  **134 passed** after explicit legacy-mode fixture corrections.
- All ten JS commands in the repository's UI/audit test pattern passed. They
  use DOM/API doubles, not Chromium customer screenshots.
- Ruff 0.15.6 and `git diff --check` passed.
- Final full changed-tree Python suite was still running when this checkpoint
  was written. An earlier changed-tree run was interrupted because its imported
  legacy fixtures predated the explicit conversation-mode corrections. A focused
  pre-correction run failed (12 failures, 34 errors); it is not a passing result.

## Live execution status

The execution workspace cannot start Chromium because Unix socket creation is
restricted. The separate cloud browser cannot reach that workspace's localhost
or filesystem. No local Raven browser screenshots have been produced yet.

The existing Daytona E2E sandbox was resumed with its existing 15-minute auto-stop
and 1-minute auto-archive retained. Docker Server **28.3.3** was verified through
its real terminal after starting its installed daemon. No new API keys were
created. This verifies infrastructure readiness only, not the new product loop.

## Acceptance still outstanding

- Actual fresh Docker/PostgreSQL install and browser requester/expert loop on
  this revision, with screenshots and adversarial interruption/recovery.
- Live Claude host inference and existing employee Slack MCP delivery. Protocol
  tests simulate those boundaries; they must not be described as live sends.
- New experts need authenticated Raven access. Default links do not silently
  grant recipient identity or create accounts. Existing optional bearer task
  capabilities remain separate. Seamless new-expert account onboarding is open.
- Native connector source ACL checks are still workspace-level, not upstream
  per-user authorization. Durable total internal-inference budgets and broader
  outbound atomicity remain open from the previous report.
- The agentswithagency.com palette was not verified; existing sage neutrals
  were retained. The UI has not yet been visually accepted in a real browser.

Accessibility design references: [W3C status messages](https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html)
and [W3C disclosures](https://www.w3.org/WAI/ARIA/apg/patterns/disclosure/).
The implementation uses native disclosures and status announcements while
keeping explicit human approval separate from clarification.

See [host notification protocol and limits](../docs/host-notifications.md).
