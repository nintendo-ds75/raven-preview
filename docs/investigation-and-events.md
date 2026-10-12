# Investigate, ask, and continue in your coding agent

Raven stays beside Claude Code, Codex or another MCP host. You give your agent an
ordinary task. The installed hook registers it. The agent discovers the files,
reads the code and tests, and sends Raven what it found. Nobody needs to provide
a decision list or move their work into Raven's UI.

## Review the investigation

`bridge_review_discovery` accepts a task ID, `observations` with actual code or
record citations, and the `paths` the host discovered. Raven reads that digest,
related records and existing decisions. It proposes questions where the evidence
suggests missing judgment. It separately identifies things the agent can verify
by inspecting code or running tests.

The model review runs in the background. `model_pending` means it has not
finished. Read `discovery_reviews` on `bridge_get_tree`, use `bridge_wait`, or read
the same review through `bridge_review_discovery(task_id, review_id)`.

For each finding, the host either creates a genuine decision node and records
its `decision_id`, or supplies a short `reason` describing what it investigated
and why another human decision is unnecessary. Both remain in the task history.
The workspace export includes review inputs, model results and dispositions so
an evaluator can reconstruct what Raven saw at that point.
The review cannot create approval or send a speculative question by itself.

On newly engaged tasks with inference enabled, finish requests a final review
using `phase=finish` and the complete current `diff`. A changed diff, decision
revision or retrieved source requires a fresh review. Existing approval gates
still apply. A failed model review says it is unavailable; it never claims
complete coverage. Repeat the original review request, or supply its `review_id`
and `retry=true`, to retry an unavailable review. Reading its ID alone only reads
the saved result. An interrupted worker becomes unavailable after ten minutes
and emits a wake event instead of leaving the host waiting indefinitely.
Invalid model output is kept
separately as rejected output, never adopted as findings. This process reviews what the host supplies. It cannot prove
the host discovered every requirement.

## Search records and human memory together

The Docker image includes Qdrant's FastEmbed runtime and the small BGE English
embedding model. Embeddings run locally. Source text is not sent to a separate
embedding service. The model files are fetched when the image is built.

Raven combines learned similarity with keyword search and exact references.
Retrieved records lead to earlier human decisions through their versioned source
links. Model selection and composition then check the proposed reuse. All source,
scope and authorization checks remain in force. A vector match is a candidate,
not a direct answer or permission.

Vectors are cached by content hash and model in the same SQLite or PostgreSQL
database. Eligibility is checked again on every search. Long documents are
chunked, and changed content gets new vectors. The first search over a large
uncached repository takes longer. This is an in-process index intended for pilot
workspaces, not a benchmarked enterprise-scale vector service.

For a Python-only installation, install `requirements-semantic.txt`. Set
`BRIDGE_EMBEDDINGS=off` to keep lexical search, or build Docker with
`INSTALL_SEMANTIC=false` to omit the runtime. Retrieval responses and traces say
when learned search is unavailable. Model selection, composition, discovery and
conversation still use your configured inference backend.

## Choose a useful first contact

Raven considers code history, listed ownership, prior conversations and authors
of relevant records. A model can compare that evidence and request one additional
search when it is insufficient. Its choice must name a supplied person and cite
that person's supplied evidence. The candidates, selected evidence and result
are recorded in the task trace.

A verified decider or explicit assignment is preserved. Refusals and scoped
contact history remain part of eligibility. Selecting someone does not give them
permanent organizational authority. They can answer or refer in Slack. Natural
phrases such as “Not me, please ask…” go through the conversation model and its
confirmation flow instead of being treated as a literal person name.

## Let Raven wake a supported host

There are two supported mechanisms:

| Host | Mechanism |
| --- | --- |
| Local Claude Code or Codex with the Raven adapter | Hooks, `bridge_wait`, and the explicitly enabled local resume supervisor described in [host adapters](host-adapters.md). |
| ChatGPT Work cloud chats and dots | MCP Events subscriptions and signed HTTPS callbacks. The user chooses the task to monitor and what to do when it changes. |

OpenAI documents MCP Events for Work cloud and dots. This is not a claim that a
generic MCP notification wakes every CLI or an arbitrary local desktop chat.
See [the official protocol integration](https://developers.openai.com/plugins/build/mcp-events).

The authenticated `/mcp` endpoint implements `server/discover`, `events/list`,
`events/subscribe` and `events/unsubscribe` for protocol `2026-07-28`. The event
name is `raven.task.changed`; its required filter is one `task_id`. Legacy MCP
clients continue using the existing tools and local adapter.

Connect the deployed HTTPS endpoint using a personal Raven API credential. In a
supported Work cloud chat or dot, ask the host to monitor the task and continue
after a person replies. The host supplies the callback URL and signing secret.
Raven verifies the callback before activating the subscription. No additional
human question or deployment is authorized merely by receiving an event.

The default subscription lease is one day, refreshable up to a seven-day lease.
Events contain task and decision IDs and the type of change. The host reads the
current task through the existing tools. They include answers, corrections,
referrals, source changes, discovery reviews and completed diff readings.

Subscriptions and deliveries survive a server restart. Retries preserve the
event ID. Raven signs the exact payload, checks credentials before each delivery,
and stops after revocation or expiration. Callback connections require HTTPS,
public destination addresses and normal certificate validation; redirects are
not followed. Secrets and callback URLs are not included in task traces or the
workspace JSON export. Protect database backups as credentials are stored there.

This event does not provide protocol replay after an expired or unsubscribed
lease. On reconnect, read the current task before subscribing. Delivery is at
least once: hosts must keep their task writes idempotent. Failed deliveries stay
in the database and appear as events in the task trace.

Protocol tests use a callback receiver. A customer's actual ChatGPT plugin
installation, subscription controls and live wake-up must still be verified in
that environment.
