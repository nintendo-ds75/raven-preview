# External record import contract

`POST /api/records` and MCP `bridge_import_record` share these rules:

- `(repo, kind, ref)` is the legacy identity. `ticket`, `jira`, and `issue`
  remain distinct kinds; an existing Jira record is never renamed to ticket.
- Text/status are refreshed. The separately frozen first compatibility fix
  preserved original author/creation metadata. With the additive version layer,
  explicitly supplied author/creation changes are captured in a new immutable
  observation; omitted values retain the previous metadata. Prior versions stay
  intact. See [versioned context memory](context-memory.md).
- Supplied `paths` is a complete snapshot. A string or array replaces the old
  paths; an empty string or `[]` clears them. Omission preserves existing paths.
- `resolved` accepts booleans, integers 0/1, or case-insensitive string
  true/false, yes/no, 1/0. Other values are rejected. Explicit false prevents
  use as settled evidence for every kind.
- A nonempty unknown or nonterminal status is conservatively unsettled, even
  when `resolved=true`. Terminal statuses include Done, Closed, Resolved,
  Completed, Merged, Accepted, Approved, Published, Effective, and Final.
  Empty status retains the legacy unqualified-record behavior.
- Source status, author, and prose never grant Raven approval, signatures,
  authority, or standing rule permission. Eligible evidence still needs the
  actual decision's sign-off.

Generic Jira and Slack import is not native Jira synchronization or full Slack
backfill. Slack Real-time Search results must remain transient and must not be
imported through this durable-record API.
