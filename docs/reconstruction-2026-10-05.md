# Restored review fixes

The cloud review's commits were unavailable. This snapshot reconstructs its changes from the saved conversation. Recorded edits were reused where available; earlier changes were rebuilt from the session summary. It is not a byte-for-byte recovery of those commits.

## What changed

* Kickoff and decision writes return a static reading while inference continues in the background. Model work cannot overwrite a newer human answer. Interrupted reads leave visible, recoverable work. MCP waits and finish reviews have bounded response times.
* An agent can resume by task ID and close an accidental task nobody has acted on. Closed questions reject late replies.
* Kickoff and decisions use consistent routing signals. Routing considers every named path, dotted module names, review activity, record authors, and handles linked to known contacts. Reusing an answer prefers its signer unless current verified authority says otherwise.
* Slack referrals understand embedded and possessive mentions. Read-backs keep reasons grounded in the message. Evidence uses readable names and complete routing explanations. Without inference, messages show working commands. Predictions distinguish their source from fresh authorization.
* Diff readings also look for policy conditions or exemptions absent from the signed answers. Located findings affect the reported verdict; missing or failed readings remain uncertain. The report is still not a test or approval.
* Setup accepts the coding project's path instead of writing MCP configuration into Raven's checkout. Development login distinguishes a named workspace from an account someone has claimed. The development MCP command uses supported arguments.
* Customer text says Raven, task labels and plurals are consistent, GitHub status reflects actual sync configuration, and documentation agrees with the tools and Slack manifest.
* Evaluations pseudonymize ingested identities before routing. Fictional fixtures use reserved email domains. Stable labels preserve identity links; they do not make public history anonymous against someone who has the original data.

## Verification on this reconstruction

| Check | Result |
| --- | --- |
| Python regression suite | 754 tests run, 4 skipped, no failures |
| PostgreSQL in Docker | 362 tests, no failures |
| Focused reconstruction regressions | 15 passed |
| Main browser, agent browser, web setup, and Slack task page | All passed in Chrome; task page covered 70 desktop/mobile steps |
| New developer walkthrough | 37 of 37 checks passed |
| Pricing wording | All 4 forms recognized |
| Repository credential scan | No real credential found; one fake PEM marker in a test fixture |

The new regressions cover delayed model responses, concurrent kickoff reads, human answers arriving during inference, interrupted work, abandonment, late Slack replies, signer routing, mention parsing, wait budgets, unsigned exemptions, and development login.

The Docker image built successfully. PostgreSQL tests ran in an isolated Compose project. Browser tests and the walkthrough used simulated connectors. Live inference, a hosted coding agent, and real Slack or GitHub installations were not rerun during this reconstruction. The historical review's live measurements are not claimed as new results here.

While this reconstruction was being verified, a separate task-page feature landed on main. Its Slack links, account flow, and web UI were preserved, the conflicts were reconciled, and the combined code was retested.

The preview main branch remains a single root commit. No license or GitHub App registration was changed.
