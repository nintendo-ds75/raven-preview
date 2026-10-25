# OpenCode execution harness (experimental)

Raven can drive the actual **OpenCode 1.19.4** native HTTP session API. This is a
second open-source execution harness, not a model endpoint branded as one. It
runs in a dedicated task home with a local authenticated server; the original
repository remains outside that home. OpenCode's native agent loop calls a
private Raven MCP bridge. Only the parent executes Raven's task tools.

## Setup

```sh
npm ci --prefix hosts/opencode --ignore-scripts
export RAVEN_HOST_OPENCODE_PROVIDER=anthropic
export RAVEN_HOST_OPENCODE_MODEL=your-exact-supported-model-id
export RAVEN_HOST_OPENCODE_API_KEY=your-dedicated-deployment-key
```

The bundled official platform binary is selected automatically on supported
Linux/macOS architectures. `RAVEN_OPENCODE_BIN` can name another locally installed
binary, but runtime health must still report exactly 1.19.4. The pinned local
npm dependencies are required even with a binary override. The worker reuses
that preinstalled dependency graph so initializing a task does not implicitly
install plugins from the network.

The selected provider must be one supported by OpenCode; provider/model are
explicit and the returned native assistant receipt is checked for both. This
does **not** mean separate integrations with every provider's coding product.
No provider key is borrowed from Raven's internal inference or a user login.

## Guardrails

- Exact binary health version and expected OpenAPI operations/receipt shape are
  checked before creating or resuming a session. Unknown schemas fail closed.
- All HOME and XDG paths are task-specific. Project configuration, discovered
  skills, default plugins, model-catalog fetches, auto-updates, LSP, formatters,
  sharing, snapshots and automatic compaction are disabled.
- Native tools are wildcard-denied. One audited local plugin rejects native
  tool execution, rejects permission prompts and annotates Raven MCP calls
  with OpenCode's native session and tool-call identities.
- The loopback MCP endpoint requires an unguessable bearer token. It accepts
  only the selected session and registered tools; missing IDs, concurrently pending duplicate IDs,
  foreign sessions and calls after interruption are refused. Completed same-ID retries pass back through Raven’s durable fingerprint/receipt ledger, never blindly repeat effects.
- Native session ID, provider and model are bound on disk. Resume never chooses
  a recent session or switches providers. A server exit, incomplete receipt or
  provider failure is not success. Interrupt calls the native abort API;
  termination also cleans up the child server with a bounded timeout.
- This is **tools-only integration, not an OS sandbox**. Raven's separate task
  command sandbox is still required for running commands safely. Do not expose
  the private OpenCode server or MCP endpoint to other users.

Native per-message cost reports are summed for new assistant messages in the
turn. These are provider reports, not a priced-dollar enforcement guarantee;
missing pricing may report zero, and interrupted/disconnected turns may lack a
complete receipt. Raven's tool-call, step and wall-time limits still apply.
Set provider-side spending limits. No automatic model/provider fallback occurs.

## What was verified

- Offline Python contracts: auth, scope, native IDs, duplicate refusal, stop,
  configuration, incompatible-version/schema rejection and interrupted startup.
- Installed official 1.19.4 binary: authenticated health, actual OpenAPI schema,
  effective guarded configuration and pre-prompt interruption.
- Installed native harness + **local scripted Anthropic SSE provider**: model
  receives exactly the Raven tool, a native call reaches the parent, the parent's
  result continues the actual agent loop, completion is read from its native
  receipt, and reopening the same session preserves prior context.
- Audited JavaScript hook tests reject native tools and permission requests.

The scripted provider is not live Anthropic inference. No live model API, real
customer repository, external action, deployment or production credential was
used. Live deployment acceptance remains pending.

Sources: [tagged OpenCode source](https://github.com/anomalyco/opencode/tree/v1.19.4),
[native server API](https://opencode.ai/docs/server/),
[tool permissions](https://opencode.ai/docs/tools/).
