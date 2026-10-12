# Real Claude Code loop on frozen urllib3

October 12, 2026. Started from `129845b012216b05e037abbed3f9364528d72114`.

The loop completed after fixes: the installed hook registered the task, Claude
discovered Raven's MCP tools, investigated the repository and connected records,
asked for judgment, received confirmed Slack answers, resumed the same session
twice, implemented the change, read the advisory diff review and saved a current
proof. The generated patch passed independent acceptance tests.

This was a supervised run with product repairs during execution, not an untouched
first-install success. Slack, Jira and documentation services were local HTTP
fixtures. Claude Code, inference, Docker, PostgreSQL, git history, the generated
patch and its tests were real. No public maintainer was contacted.

## Setup and boundary

* Raven's `./setup` installed the Docker stack and project hooks. The database was
  PostgreSQL 17. Claude Code 2.1.296 ran in a separate container with its normal
  hooks, MCP connection and `ToolSearch`; it was not a scripted stand-in.
* urllib3 was frozen at `3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, before
  October 11, 2025. Its sealed checkout had 4,299 commits and 22,449 objects, with
  no remotes, alternates or future refs. Raven indexed 2,000 commits and 162 files.
* The host could reach Raven and inference but could not fetch future GitHub
  history. The private policy oracle and acceptance tests were outside its mount.
  This does not exclude knowledge already in the model's training data. The Raven
  server's repository mount was broader than the host's, so this is not a claim
  of filesystem isolation between the server and evaluator.
* The task was synthetic business work on real historical code, not recovery of
  a held-out upstream commit. Oracle and independent-test hashes were recorded
  before the host ran. The prompt named no files, owners or decision list:

  > Our services sometimes receive Retry-After delays longer than they can afford to wait. Add an optional caller-controlled upper bound for Retry-After sleeping without unexpectedly changing existing retry behavior. Investigate, implement, document and test the change. This is for the library-next release.

* There was no manually entered authority map. The simulated Slack directory had
  280 contacts, derived from pseudonymized repository identities plus two synthetic
  policy contacts. An ingested policy named Mira for runtime questions and Theo for
  release documentation. This is useful cold-start context, not a no-context test.
* The real connectors imported two Jira records, one Slack policy message and two
  documentation sections. A restricted Jira issue and a robots-disallowed page
  were refused. NET-102 was a cancelled proposal for a universal 19-second default;
  the implemented result preserved the unlimited default.

## What happened at each step

| Step | Observed result |
| --- | --- |
| Hook | The prompt hook registered task `66ab400660f4` before Claude's repository investigation. Stop hooks ran at the waiting and completed stages. |
| Tool discovery | Initially broken for the modern protocol. After the envelope fix, the actual host discovered all 21 tools and called them. |
| Investigation | Claude initially drafted a cap in the parser. It read the ingested policy through MCP and corrected the draft to cap only sleeping. |
| Discovery review | Cold retrieval initially delayed the call. Background retrieval now starts after a durable pending receipt; later live receipts returned in 41–49 ms. A separate blocked-encoder regression tests the cold path. |
| First contact | Runtime policy reached Mira from source evidence. The documentation follow-up initially reached a repository-history candidate and needed a handoff to Theo. |
| Natural replies | Mira's “I'm in a meeting” reply granted no authorization. Full answers and a required follow-up were interpreted by live inference, read back and confirmed with their exact codes. |
| Handoff | “Not me, please ask Theo just this one” was confirmed and applied to that question. It did not grant broad ownership. |
| Wakeup | The installed local supervisor leased the event, launched real `claude --resume` for the original session, then acknowledged and posted its report. Both resumes exited successfully. No evaluator-supplied implementation prompt was sent on resume. |
| Implementation | Claude added `retry_after_max`, an unlimited default, finite/nonnegative validation, zero-cap handling, cloning support, tests, API documentation and a changelog fragment. |
| Finish | Pending discovery held finish. Claude addressed the final findings, received the asynchronous diff reading, amended documentation, re-reviewed the new diff and refreshed the saved proof. |
| Review delivery | The final diff review took 122.6 seconds. Its first finish call returned at 45.3 seconds; waits delivered the terminal reading. A repeated finish reused it in 0.21 seconds. |
| Proof | The saved 7,839-byte diff exactly matched `git diff HEAD`. Its SHA-256 is `8309f0a2816c5089c89ad51d248d1de57b82cf00a49a94058e51347868a6ed22`. It includes two answered/authorized decisions and the adopted follow-up placeholder. |
| Browser | The real accountless Slack link displayed both signed answers, requester, handoff, history and people involved. No account was needed to view that task. |

The final tree had zero blocking nodes and the exported proof had
`review_pending=false` and `review_snapshot_current=true`. Test claims inside the
proof remain explicitly host-reported; the separate evaluator ran its own tests.

## Product fixes from the run

1. **Modern MCP tool discovery.** Raven advertised the 2026-07-28 protocol but
   omitted required response-envelope fields. Claude connected yet could not
   discover usable tools. Results now use the requested version, including
   complete/cache metadata, and preserve legacy responses. Conflicting versions
   are rejected. The actual host subsequently discovered and used the tools.
2. **Cold retrieval before the discovery receipt.** Embedding work ran before a
   review was saved or returned. The request now records a source-revision snapshot
   and pending review first; retrieval and inference run in the background. The
   exact selected evidence is stored before inference. Changed source context
   cannot silently pass as the original snapshot.
3. **Overly brittle discovery validation.** A cited question was discarded because
   the model named a file the host had not listed. Such paths now remain explicitly
   unverified annotations, without becoming scope or routing authority. A later
   review cited an exact supplied decision ID without its `decision:` prefix;
   that exact alias now resolves, while unknown IDs and prefix matches still fail.
   The prompt also explains that adopted follow-up placeholders are not unanswered
   duplicate decisions. The final alias fix has regression coverage; the host's
   final successful live review already used valid full IDs before that fix.
4. **A named recipient lost during follow-up interpretation.** The model's rewritten
   question omitted Theo. The confirmed original request is now preserved through
   follow-up creation and adoption, and shown in the read-back. The prompt also
   asks for named recipients to be retained. Regression tests deliberately omit
   the name in model output. This live question was created before the fix and
   recovered through a real simulated handoff; improved first-contact routing on
   a newly created follow-up was not separately remeasured.
5. **A complete approval unnecessarily forced out of Slack.** A shared 12 KB limit
   rejected Theo's source-bound read-back. Slack now has a conservative 36 KB byte
   limit, still bounded by block limits, while Teams keeps the smaller limit. The
   same answer was retried, delivered whole at 19,719 bytes and confirmed in Slack.
   A provider `message_truncated` warning now prevents a delivered confirmation
   from being recorded. That warning path has a regression test, not a real Slack
   workspace test. Boundary tests measure the final rendered text, including rule
   effects and the confirmation code; an earlier estimated allowance unnecessarily
   rejected a valid message exactly at the limit and was removed.

The protocol envelope follows the [MCP tools specification](https://modelcontextprotocol.io/specification/2026-07-28/server/tools).
The Slack limit leaves room below the documented text truncation limit; long
messages still have block-specific limits. See [chat.postMessage](https://docs.slack.dev/reference/methods/chat.postMessage/).

## Harness failures, kept separate

Initial attempts hit bind-mount git ownership and a launcher tool allowlist that
omitted `ToolSearch`. Those were test-environment mistakes. The clean host run used
a fresh working tree and disabled automatic Claude memory. An early tracing proxy
also buffered SSE and mislabeled it as JSON, producing disconnections and needless
long waits. The repaired proxy forwarded streaming bytes and content type directly;
24 progress notifications are retained in the trace. Those transport failures are
not attributed to Raven. Abandoned attempts are identified in the evidence.

## Validation

* Final urllib3 patch: **127 passed, 1 skipped**, comprising 11 private acceptance
  cases plus the retry and pool-manager suites. The private cases were not mounted
  into the coding host.
* Changed PostgreSQL contracts: **148 tests, 28 skipped, passed** in isolated
  schemas, including discovery, events and Slack source-review cases.
* Seven JavaScript DOM/API suites passed. These are test doubles, separate from
  the real browser observation above.
* The production Docker image rebuilt successfully with the fixes.
* An intermediate full run found four tests still using the previous Slack limit.
  Their updated multibyte and final-message boundary cases pass in a 163-test
  targeted run (60 skipped). The final suite result below is a separate run.
* Full final Python suite: **2,364 tests, 138 skipped, passed** in 530.4 seconds.
  The rebuilt image passed **198 focused tests, 60 skipped**. Ruff and credential
  scans passed. Exact outcomes and publication checks are recorded in
  [validation.json](../evals/newdev/results/claude-loop-2026-10-12/validation.json).

## Remaining friction and limits

The docs question needed a handoff despite source text naming the contact. This
run establishes recovery, not reliable first-contact accuracy. The Slack review
was complete but far too long to feel lightweight, with literal escaped identifier
characters and substantial scope/source text. There were 22 outbound messages
across the repaired run, including confirmation exchanges and supervisor reports.

The final diff reading was conservative: both decisions were `unclear`, with no
confirmed departures. Unchanged `increment()` code was outside the supplied diff;
one documentation citation also failed the exact-line check. Claude's summary
described only the unchanged-code limitation, understating the second caveat.
The saved review retains it. Independent tests establish the tested runtime
behavior, not a general guarantee about the change.

The changelog number was a host-chosen placeholder for a synthetic task, not an
assigned upstream PR. This patch was not submitted to urllib3. The stopped-host
wake test used Raven's installed local supervisor with a shortened stop-hold
interval. It does not demonstrate unsolicited wake support in every host or a
live cloud MCP Events subscriber. Real Slack installation, OAuth, provider rate
limits, Jira tenant permissions and production traffic remain untested here.

[Redacted evidence and reproduction notes](../evals/newdev/results/claude-loop-2026-10-12/README.md).
