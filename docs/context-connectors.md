# Connected context and human memory

Raven keeps external evidence and people's decisions in one versioned graph.
A document excerpt links to the question it informed, the answer, the people
who approved it, and the task that used it. A later task can retrieve the answer
through that source relationship even when the question uses different words.
A match proposes evidence. It never gives the new task the old task's approval.

## Optional Airweave connection

[Airweave](https://github.com/airweave-ai/airweave) supplies open-source connectors
and hybrid search. Raven uses its documented
[instant search API](https://docs.airweave.ai/api-reference/collections/instant).
Airweave handles its source synchronization and search index. Raven imports the
returned excerpts into its own canonical source/decision graph. Human answers
are not sent to a separate search service or detached from their source versions.
Native GitHub ingestion and Slack replies continue to work without Airweave.

Use a customer-managed Airweave deployment or account. Follow its current
[self-hosting instructions](https://github.com/airweave-ai/airweave) and connect
only the sources the customer has approved. This repository does not deploy
Airweave or silently configure OAuth grants. Its setup and resource requirements
are separate from Raven's two-container installation.

### Connector compatibility comes before configuration

A connector's presence in Airweave does not establish compatibility with this
adapter. Raven needs each durable result to carry a stable sync identity and
explicit access metadata. At upstream Airweave revision
[`1ebe1af`](https://github.com/airweave-ai/airweave/tree/1ebe1af2dbfb90f3334410721e69997e4f02b320),
the [GitHub connector](https://github.com/airweave-ai/airweave/blob/1ebe1af2dbfb90f3334410721e69997e4f02b320/backend/airweave/platform/sources/github.py)
and [Jira connector](https://github.com/airweave-ai/airweave/blob/1ebe1af2dbfb90f3334410721e69997e4f02b320/backend/airweave/platform/sources/jira.py)
do not populate the required access field. The upstream
[base entity](https://github.com/airweave-ai/airweave/blob/1ebe1af2dbfb90f3334410721e69997e4f02b320/backend/airweave/platform/entities/_base.py)
defaults that field to `null`. Such results are rejected, including results with
a public-looking GitHub URL. Do not fill in `is_public: true` or invent viewer
IDs merely to make an import succeed. Raven's native GitHub ingestion is a
separate supported path. Generic Jira imports remain caller-refreshed.

The repository's ACL-bearing HTTP fixtures validate Raven's admission contract;
they do not certify those upstream connectors. Before choosing a collection,
check an actual result from the exact deployed connector version. If it omits
access metadata, that connector is not currently usable through this durable
adapter. Airweave's federated Slack search remains transient and is excluded
regardless of its access fields.

1. Create a collection containing material shared with **every reader of this
   Raven workspace**, including people who receive personal task links.
2. In Raven's private `.env`, set `BRIDGE_AIRWEAVE_URL` to the API base URL and
   `AIRWEAVE_API_KEY` to its credential. Use HTTPS outside a local test. The API
   base must include any deployment-specific prefix. Restart with `./dev up`.
3. Register the collection. Supply the source principal IDs used in the returned
   ACLs, not display names or Raven IDs:

```sh
docker compose exec -T app python -m bridge.context_connectors \
  --repo your-org/your-repository --collection team-context \
  --workspace-shared --audience member-id-a,member-id-b
```

An empty audience accepts public sources only. For private results, the returned
`viewers` must include **all** configured audience IDs, and `is_public` must be
explicitly false. Unknown ACLs are rejected. Group membership is not expanded by
Raven. If a connector only returns a group ID, resolve membership or use a
separately scoped workspace; do not enter the group as a substitute for readers.
The operator must update the audience before adding readers. This is a
workspace-sharing contract, not automatic per-user permission synchronization.

4. Ask the host to call `bridge_connection_status`, then
   `bridge_search_context(repo=..., task_id=..., query=...)`. The result names
   imported and rejected observations, current sources and related decisions.
   Kickoff and decision creation also search a configured collection automatically.
5. Check a result before the pilot. It must contain the expected text, source
   identity, update time and access metadata. A successful HTTP response alone
   does not demonstrate useful retrieval or correct source permissions.

Search returns bounded rejection reasons and counts, including
`missing_access_metadata`, `access_not_shared` and `transient_or_unsynced`.
When every returned excerpt is rejected, the error also appears in connection
status as `needs_attention`; it is not a healthy retrieval result. Mixed results
retain the accepted excerpts and explicit rejection diagnostics. An empty search
is distinct from an incompatible response. Diagnostics never include rejected
source bodies or credentials.

Disable with the same registration command and `--disable`. Changing collection,
audience or enabled state retires the old retrieval grant. Credentials stay in
the server environment; they are not stored in source records or task traces.

## What is stored and how it expires

The durable identity is collection + source name + original entity ID + chunk
index. Text, status and access metadata identify an observed revision. Raven
labels each result as an **excerpt**, not a complete source document. Every
accepted excerpt is a normal versioned source record, with task anchors and
decision evidence edges. Graph traversal nominates signed answers for reuse;
the normal relevance, scope, conflict and approval checks still apply.

Each successful retrieval has a five-minute freshness lease. The server worker
checks observations nearing expiry every 30 seconds, up to 40 per repository per pass,
using exact identity filters. Retrieval and finish checks reject expired evidence
even if the worker has stopped. A recovered identical source restores usability;
a material source change or changed access grant requires current-source review.
Historical source versions and signatures are retained as history.

This is bounded staleness, not an instantaneous revocation guarantee. Source
changes can remain unseen during the lease and during Airweave's own sync delay.
Missing filtered results mean unavailable, not deleted. Raven does not infer
whole-document deletion from a search response. A transient failure blocks reuse
until a successful refresh; it does not manufacture a source revision. An actual
permission change invalidates dependent approval. Large or malformed excerpts
are rejected whole rather than truncated.

Slack search is transient. Federated results, results without a sync identity,
and Slack excerpts are not imported by this adapter. Raven's own Slack bot still
records the decisions people explicitly give it. Its bounded [capture callback
path](slack.md) follows edits/deletions only for those explicitly enrolled message
identities. It does not turn search results into durable company memory or
backfill Slack. Generic caller-imported Jira and other records remain
caller-refreshed; their `latest_observation` is local acceptance, not proof of an
upstream synchronization.

## What is visible

`bridge_search_context` records the query, selected source versions, related
answers, rejected-result count and failure state. The task graph and source
history show which evidence an answer relied on. `bridge_connection_status`
reports the collection, generation, last successful search and errors.

Connection state distinguishes `configured_unverified`, `search_observed`,
`needs_attention` and `disabled`. The backward-compatible `last_success` timestamp
means a successful search transport, not proof that any result was admitted or
that permissions were independently checked. Configuration and the operator's
reader list are declared prerequisites, not live connector certification.

An owner can review complete current evidence from the personal task link in
Raven's Slack DM. The link remains scoped to its task and its recipient's
standing. A source change between reading and signing refuses the old review.
The existing link is a bearer credential; forwarding it forwards its access.
No new workspace-wide access is granted by opening a source review.

## Validation boundary

The adapter is tested against the published HTTP schema with a local simulated
Airweave service, including unknown permissions, revocation, changed text,
expiry, missing results and redirects. It has not been certified against a
customer's live Airweave installation. Before a pilot, verify that the chosen
connectors emit stable chunk identities and complete ACLs, measure retrieval on
that team's questions, and exercise a real upstream permission change.
