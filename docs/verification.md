# What we have tested

The latest [new customer loop on 5 October 2026](customer-loop-2026-10-05.md) used a fresh Docker/PostgreSQL install, a real Claude coding host and inference, urllib3, simulated Slack and computer-use browser testing. It completed six signed decisions and a working patch without a manually prepared ownership map. It also found and fixed conversation and review failures. Scope continuity in learned routing and the limits of model review remain open. The report and evidence distinguish the successful path from the failures and retests.

The coding-host run on 4 October 2026 started with a fresh Docker installation, PostgreSQL 17, a clean urllib3 checkout, and a real Claude Code agent over MCP. The task asked for an optional total retry time budget. It supplied no file paths, owners or decision list.

The agent found four decisions, adopted a required follow-up, received runtime and release signoffs, and implemented the change. A fresh agent recovered the same task and corrected two failures found by independent checks. The final patch passed 292 relevant urllib3 tests (one skipped) and all 20 independent behavior checks. The first patch had passed its own tests but failed two independent checks; this was a supervised run with fixes, not an uninterrupted success.

The run found and fixed incomplete approval requirements after an invalid dependency, two wait cases that stalled despite available work, a rule condition denial bypass, a plain-language compatibility signal missed at kickoff, and a review that could say follows when its counterexample check failed. The last case now reads unclear, verified live: three failed checks were labeled unclear rather than follows. That makes failures more honest; it does not make model review proof of correctness.

The browser was exercised on desktop and at 390px width. MCP connection comes first; manual requests and optional integrations are disclosed only when needed. Task overview, history, saved review and a stakeholder note returned through MCP were checked. A mobile sidebar regression was caught and fixed during this pass.

Restart preserved decisions, signatures, notes and the saved review. Backup restored all 39 tables with matching counts and exact checks of decisions, tasks, revisions, accounts and authority.

Checks: 590 Python tests run with 7 skipped; 215 PostgreSQL contracts; all 31 finish tests after the final review fix; web setup checks; and the 37-check deterministic onboarding walkthrough. This pass used computer-use browser testing rather than claiming a fresh run of both standalone browser suites.

The repository, coding agents and inference were real. People and their authority map were synthetic. Slack was simulated in the walkthrough, and ticket records were injected through the record API. No real external connector installation was verified.

The remaining concern is semantic reliability. The first review missed two executable bugs, and each full review took over three minutes. Discovery is not guaranteed; the first contact can need correction; independent code tests and human review remain necessary. The Slack discovery update below removes the requirement to prepare a manual ownership map. Raven is suitable for a supervised pilot, not unattended shipping.

## Automatic discovery and Slack replies, 4 October 2026

A separate fresh Docker/PostgreSQL 17 instance ingested a clean urllib3 checkout at `ed0ed075c6b93f7c515ebd3abe9a7248507ef8c5`. A simulated Slack Web API supplied 72 contacts derived from public commit identities, across several directory pages. No ownership rules or personal Raven accounts were entered. The extra person in the database was the local Docker operator identity.

A scripted MCP client submitted a retry-policy task, then supplied the file it had located in the checkout. Raven inferred a contributor, explained the history supporting that choice, and queued a DM. Finishing was refused before the answer. A signed Slack Events API callback recorded the answer; the client received it through the wait/tree tools and finished. The contact was an inferred first contact, not independently verified as urllib3's policy authority.

Two additional cases passed: a Jira record imported through the limited MCP credential routed to its author without local repository history, and an unknown topic went to the triage channel, received an explicit referral, then reached the selected person's DM. Claiming a question did not approve it or create authority. The database ended with zero personal passwords and zero authority rows.

The computer-use check opened the optional web UI, verified automatic contact discovery and the corrected setup wording, and successfully refreshed all 72 contacts. The UI no longer instructs users to create owners before routing. The README and Slack setup guide now explain the one-time app installation, required scopes, reachable callback URL, source ingestion and optional browser access.

The 18 new discovery tests passed on both SQLite and PostgreSQL. They cover identity collisions, preserved admin roles, deactivated members, paging, failed sync, references to Jira records, referrals to new Slack members, duplicate claims, optional browser onboarding, and a stdio writer sharing the Slack worker's database. The complete Python suite contains 608 tests, including four environment-dependent skips.

This run verified transport, ingestion, routing and authorization. It used a scripted MCP client and simulated Slack, not a live coding model or a real Slack installation. It did not implement a new urllib3 patch or repeat the earlier semantic evaluations. GitHub sync already exists; Jira and historical Slack content enter through records supplied by a host connector or the records API. There is no new built-in Jira polling service or automatic Slack archive backfill.

## Natural Slack conversations and learned contacts, 5 October 2026

The final live run passed 21 checks on the same clean urllib3 checkout, using 500 commits and 72 simulated Slack contacts derived from public commit identities. It used real Anthropic inference for conversation, real repository ingestion, signed HTTP callbacks and the actual MCP tool handlers. A scripted client drove the host side. Nobody from urllib3 was contacted, and the selected contacts were not verified as its real decision makers.

The first person asked why a decision was needed, then referred it in ordinary language. The second person initially gave a non-answer, then answered and added a qualification. Raven read back both requirements, ignored “OK” as approval, and retained the pending read-back across a server restart. A duplicated confirmation produced one answer. The host read it and finished. A second task reused the answer and reached the final respondent, while requiring fresh sign-off. The database had zero authority rules and zero recipient passwords; learned contacts were stored separately from authority.

Testing caught two conversation problems before publication: unused JSON fields could arrive as null and cause a valid reply to be discarded, and a model could mistake an amendment for agreement. Null optional fields are now normalized. A non-confirmation labeled as agreement is interpreted again; if it remains ambiguous, the old read-back is discarded so a later yes cannot sign it. Regression tests cover both. Ordinary conversational referrals now learn a scoped first contact rather than implicitly granting ownership over a directory. Answering also no longer accepts an unrelated pending referral from another repository.

The Docker/PostgreSQL loop separately passed ingestion, directory discovery, an inferred DM, a refusal to finish before an answer, signed replies, host resume, Jira-record routing, triage, and restart persistence. This container run used deterministic replies. The real-inference conversation above ran on the host with SQLite. No new urllib3 implementation patch was produced in this pass.

Validation: the complete Python suite ran 635 tests with four environment-dependent skips. The PostgreSQL suite passed 257 contracts before the final amendment guard; all 27 conversation, scope and queue tests then passed against the final Docker image on PostgreSQL. Coverage includes duplicate callbacks, competing reply workers, failed acknowledgements, expired processing leases, reply ordering, required follow-ups, co-signatures, one-off referrals, customer scope and search-result non-retention.

Slack's current manifest and Real-time Search request shape were checked against its official documentation. Search was tested against a simulated API, including a canary proving that returned text and action tokens do not enter the database. A real Slack installation, real search permissions and private-channel OAuth remain unverified. Bot-only search is limited to public channels during a Slack interaction. Voice remains future work.

The [recorded checks and conversation](../evals/newdev/results/slack_conversation_live.json) contain synthetic decisions, not statements by the named public contributors.

To repeat the live conversation test with your inference credentials:

```sh
python3 evals/newdev/slack_conversation_live.py \
  --repo /path/to/urllib3 \
  --output /tmp/raven-conversation-new-run
```

Use a new output directory. The script writes a database and transcript there. It calls your inference backend and can incur charges; it does not send messages to real people. It isolates conversation behavior by keeping the resolution ladder deterministic. Passing this run is evidence for a supervised pilot, not a guarantee that every natural-language message or ownership inference will be correct.
