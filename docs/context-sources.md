# Native context sources and the onboarding report

Raven's graph links sources, decisions, people and tasks. GitHub has always been
synchronized natively. This page adds three opt-in sources that use the same
pattern (Slack public channels, Jira projects, documentation sites) and a
report that tells an administrator and a coding agent what Raven can read.

## Why native connectors

Raven admits a durable source only when it knows the source's identity, version
and who may read it. The alternatives were measured against that:

| Option | Why it is not the default |
| --- | --- |
| Airweave | Its GitHub and Jira connectors return no access metadata at the pinned revision, so Raven rejects them. Its Slack search is federated and transient. It also needs its own Postgres, Temporal and vector store. |
| Onyx | Same polling-plus-permission-sync shape, but a heavy deployment (Vespa, Celery). Raven reuses the design, not the service. |
| Official remote MCP servers (Slack, Atlassian Rovo, GitHub) | They act with each caller's own access and return transient results, so nothing they return can anchor a signed decision. They remain useful live companions in the host. |
| Temporal graph and memory libraries (Graphiti, Zep, Mem0, Cognee) | No per-document permission model, and they need a graph database or an extraction model. Raven already versions sources and decisions. |

So each source is a small standard-library poller that writes through the
existing `Store.add_record` contract, the path GitHub, Airweave and Slack
capture already use. A record keeps its source identity, version, timestamps
and a link back to its origin. A changed source becomes a new version and puts
dependent approvals up for review, exactly as for other sources.

## The shared rule: only what every reader may see

Raven has one audience: every reader of the workspace, including people who
receive personal task links. A source is admitted only when an administrator
attests that its content is shared with that whole audience. This is the same
`--workspace-shared` attestation the Airweave adapter requires. Each connector
then fails closed on anything narrower:

| Source | Admitted | Refused |
| --- | --- | --- |
| Slack | Messages and thread replies in a public channel an administrator selected | Private channels, DMs, group DMs, Slack Connect (externally shared) channels, a channel that becomes private or archived |
| Jira | Issues in a selected project, and their unrestricted comments | Issues with a security level; comments restricted to a role or group |
| Documentation site | Pages on the selected origin found through `llms.txt` or `sitemap.xml` | Other origins, pages `robots.txt` disallows, non-text content |

Every refusal is counted by reason and shown in the report. It is never
silently imported with a narrower label.

## Synchronization pattern

All three follow GitHub's sync:

1. Read from the stored cursor minus an overlap window (Slack 5 minutes, Jira
   5 minutes, documentation by validator and content hash).
2. Finish the network reads, then write in one transaction.
3. Advance the cursor only after the write commits. A failure records the
   error, keeps the cursor and is retried on the next pass.
4. Writes are idempotent: the same content at the same identity only updates
   when it was last observed. Changed content becomes a new version.

A background thread runs every `BRIDGE_SOURCE_SYNC_MINUTES` (default 15). An
administrator can run one source immediately.

### Slack public channels

This is an opt-in **backfill**: Raven keeps its own versioned copy of a public
channel's history, starting `BRIDGE_SLACK_BACKFILL_DAYS` (default 30) back.
Without a selected channel, nothing changes: Raven still stores only the
decisions people give it, and live search results remain transient.

- Raven runs as each customer's own internal Slack app. Slack's API terms
  forbid persistent copies for commercially distributed apps; they do not
  apply to an app created for a single organization's own use. Confirm this
  with your counsel before enabling it.
- Scopes: `channels:read` to confirm a channel is public on every pass, and
  `channels:history`, which the pilot manifest already grants.
- `conversations.history` from the cursor, paged. Threads are followed with
  `conversations.replies` while they stay active (14 days by default),
  because a new reply does not return its parent to the history window.
- Edits are picked up within the overlap window and while a thread is
  followed. Deletions are not visible to polling. The record keeps its last
  observed version, and the report says so.
- Bot messages, including Raven's own, and channel-join notices are skipped.

### Jira projects

- Jira Cloud with an API token (`JIRA_BASE_URL`, `JIRA_EMAIL`,
  `JIRA_API_TOKEN`). API-token traffic has burst limits, not the points
  quota that applies to OAuth apps.
- `GET /rest/api/3/search/jql` with `updated >= <cursor - overlap>` in epoch
  milliseconds, ordered by `updated` then `key`, paged with `nextPageToken`.
  The removed `/rest/api/3/search` endpoint is not used.
- A record per issue: summary, description, status, resolution, reporter and
  unrestricted comments. Rich-text (ADF) fields become plain text. When the
  comments do not fit the record limit, the newest ones are kept and the
  record states how many earlier comments were left out.

### Documentation sites

- Discovery in order: `llms.txt`, then `sitemap.xml` (including sitemap
  indexes), then the page itself. Links stay on the registered origin.
  `robots.txt` is honored.
- Conditional requests (`If-None-Match`, `If-Modified-Since`) and a content
  hash decide whether a page changed. HTML becomes text with headings kept.
  A page is split into sections at headings, each a record linked to the
  page's URL. A section that disappears, or a page that returns 404 or 410,
  is marked deleted, not erased.
- Bounded: at most `max_pages` pages per site (default 200) and 2 MB per page.

## The onboarding report

`bridge_context_sources` (MCP), `GET /api/context/sources` and
`python -m bridge.sources status` return one report. For each source it lists:
whether credentials are present, whether Raven verified access, the last
successful sync and error, how many records it holds, refusals by reason, and
what to do next. Unconfigured sources are listed with the setting that would
enable them, so a new agent session learns what context exists, and what does
not, in one call. Sources that need attention also appear in readiness.

The report states configuration and observed results. It does not claim a
source is complete: polling cannot see deletions, and a refused item is
absent by design.
