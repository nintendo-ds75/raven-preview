# Execution harness coverage

These are execution harnesses, not aliases for Raven's internal model calls.
Claude, Pi, OpenCode and the narrowly qualified experimental Codex adapter use
Raven's durable task, approval, file/tool and uncertain-intent boundaries. Codex
requires the exact native version and model below; its advertised native
question tool is denied, not converted into a Raven judgment.
No adapter adds permission to publish, deploy, or approve its own output.

| Harness | Integration | Verification in this change |
| --- | --- | --- |
| Claude Agent SDK 0.2.165 | Existing in-process Raven MCP tools; native tools denied | Existing offline contracts; no new live call |
| Codex app-server 0.162.0-alpha.17.2 | **Experimental**, only `gpt-5.3-codex`; parent-mediated work tools, native questions denied | Actual native scripted loop/context/resume, interruption and adversarial refusal tests passed in CI; public-entrypoint requalification pending |
| OpenCode 1.19.4 (MIT/open source) | Guarded native HTTP sessions + private Raven MCP bridge | Installed native tool loop/resume with local scripted inference; no live provider call |
| Pi SDK 1.4.0 (MIT/open source) | Pi's actual session/agent loop, explicitly allowlisted Raven custom tools, persistent session resume | Fake contracts plus installed SDK tool loop/resume with scripted inference |

See [OpenCode setup and guardrails](opencode-harness.md) for the separate OpenCode adapter.

Pi supports its own provider/model catalog (including OpenAI, Anthropic, Google,
Mistral and hosted open-weight models through supported catalog providers).
These are inference destinations **through Pi**, not integrations with those
vendors' competing coding products. Raven does not claim that every catalog
model supports reliable tool use or that every provider was live-tested.

## Pi setup

Node.js 22.19 or newer is required. In the Raven checkout:

```sh
npm ci --prefix hosts/pi --ignore-scripts
export RAVEN_HOST_PI_PROVIDER=google
export RAVEN_HOST_PI_MODEL=your-exact-pi-catalog-model-id
export RAVEN_HOST_PI_API_KEY=your-dedicated-deployment-key
```

Select **Pi SDK (open source)** in New task. The Pi SDK runs on the Raven server,
not a user desktop. It is a real third-party open-source execution harness; it
is not an implementation of a home-grown model loop. Only the dedicated key is
passed to it; no inference credentials are silently borrowed. Missing package,
model or key blocks selection. Authentication and model availability are only
established by an explicitly run live task, not by the setup indicator.

The worker disables extension/skill/prompt/AGENTS discovery, native tools,
automatic retry, compaction and cache warming. It checks the actual active tool
list before prompting. Session files live under the task's dedicated host home;
resume refuses a path outside that task's session directory. Provider reasoning
and raw errors are not forwarded. Stop requests abort the session and pending
tools. EOF, unknown model or provider fallback never counts as success.

The Pi process itself is not an OS sandbox. As with Claude, task commands run
only through Raven's separately configured command sandbox. The qualified Codex configuration is tested for native effect refusal, but is not an OS isolation boundary. A provider-reported dollar amount is not a billing
ledger: the Pi threshold is checked after each assistant response and can
overshoot by one response; missing provider pricing can report zero. Tool-count
and host-wall-time limits still apply. Review live usage and set provider-side
limits. Codex still has no priced-dollar enforcement.

## Protocol references

- [Pi 1.4.0 SDK source](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/src/core/sdk.ts)
- [Pi full-control SDK example](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/examples/sdk/12-full-control.ts)
- [Pi persistent sessions](https://github.com/earendil-works/pi/blob/v1.4.0/packages/coding-agent/examples/sdk/11-sessions.ts)
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)

No provider credentials, live inference, customer data or external publication
were used by the new tests. A scripted SDK/server is deliberately labeled fake;
its passing tests are not live provider acceptance.

### Self-hosted/open-weight inference through Pi

Pi can use an explicitly configured OpenAI-compatible local server (for example,
an operator's Ollama/vLLM deployment). This is still the **Pi execution harness**,
not a new Ollama execution harness. No model downloads or server provisioning
happen automatically.

```sh
export RAVEN_HOST_PI_PROVIDER=raven-local
export RAVEN_HOST_PI_MODEL=your-tool-capable-model
export RAVEN_HOST_PI_BASE_URL=http://127.0.0.1:11434/v1
export RAVEN_HOST_PI_MODEL_API=openai-completions
export RAVEN_HOST_PI_API_KEY=local-not-secret
```

`MODEL_API` may also be `openai-responses` or `anthropic-messages` when the endpoint
actually implements that wire protocol. Remote endpoints require HTTPS; HTTP is
limited to loopback. URLs cannot carry credentials or query strings. The custom
model profile is text-only with a 32,768-token context and 4,096-token output
ceiling; choose a model/server that supports those limits and tool calls. Custom
endpoint pricing is unknown (reported zero), so the dollar threshold is not a
cost guarantee; tool-count and wall-time limits remain active. Use provider-side
limits for hosted custom endpoints.

The selected provider/model/endpoint is pinned in the task's private host home.
A changed endpoint cannot silently receive an existing transcript: start a new
task for that change. Offline tests exercise real Pi registration and execution
with scripted inference; no actual Ollama/vLLM/model service was contacted.

## Optional Docker runtime

The default Python image ships adapter source and manifests, but deliberately
does not install Node/Pi/OpenCode. To include their pinned dependencies:

```sh
docker build --build-arg INSTALL_HOSTS=true -t raven-core-base .
docker build -f docker/Dockerfile.harnesses -t raven-workbench-base .
docker compose -f compose.yaml -f compose.workbench.yaml up --build -d
```

The explicit harness image copies Node 22.19.0 from its official Debian image
and installs the three npm lockfiles with install scripts disabled. The existing
workbench overlay still grants Docker control only to the trusted Raven server,
not to any model or harness process. Set the host variables in the deployment
environment, never in the image. Loopback custom model endpoints refer to the
container itself, not the Docker host. Use an operator-managed HTTPS endpoint
when inference runs elsewhere.

This change's local environment did not have Docker; the Dockerfile was reviewed
but image build and container acceptance were **not run**. Native pinned package
initialization and scripted-inference tests are separate from Docker acceptance.

## Exact Codex native acceptance

The experimental adapter is qualified only for official npm
`@openai/codex@0.162.0-alpha.17.2` and model **`gpt-5.3-codex`**. Both capability
selection and the worker independently reject a different binary version or
model. There is no unsupported-model override. Native tools can vary by model;
a different model/version requires new native acceptance, not just a config edit.

```sh
npm ci --prefix hosts/codex --ignore-scripts
export RAVEN_HOST_CODEX_MODEL=gpt-5.3-codex
export RAVEN_HOST_CODEX_API_KEY=your-dedicated-deployment-key
```

Raven prefers this locked package after an explicit `RAVEN_CODEX_BIN` override
and before a system CLI; overrides must still report the exact qualified pin.
The optional harness image includes the package. Authentication/model account
access and real inference are not established by an offline setup indicator.
The adapter has no priced-dollar enforcement; use provider-side cost controls.

The same transport configuration runs from the public `codex()` entrypoint in
production and native CI. A temporary test HOME, dummy key and loopback scripted
Responses SSE service ensure CI does not use a real account or live inference.
Both start and resume validate the effective returned model/provider and session
identity before accepting a turn. The actual resume schema has no
`allowProviderModelFallback` field; returned identity is checked instead.

The complete advertised namespace/name/type/schema is pinned in a test snapshot.
Besides Raven's dynamic work tools, upstream still advertises
`request_user_input`. A valid native question is explicitly refused; no answer
is fabricated and it never becomes a Raven approval. Native goal creation and
updates are disabled/refused, not treated as harmless bookkeeping. Task effects
remain mediated by Raven tools and its durable intent ledger. This is not a claim
that the binary has zero native capabilities or is itself an OS sandbox.

Native acceptance checks fresh and resumed processes for:

- positive dynamic tool IDs/results, retained context and exact tool metadata;
- terminal interruption of a pending tool followed by same-session resume;
- explicit call-ID/tool-matched native refusal of shell file/key reads, image
  reads, shell network attempts, a second `shell_command` network entrypoint,
  correctly formatted freeform `apply_patch`, native questions and goal changes;
- unchanged synthetic canaries, no loopback sink hits, and no forbidden native
  command/file/network/image/MCP/subagent execution lifecycle or delta events.

Only complete native dispatch refusal receipts qualify: missing output, command
crashes, OS PermissionErrors and unrelated/wrapped refusal text fail. These
network tests exercise native shell entrypoints and a local sink, not an invented
`web.run` protocol or an actual external service.

```sh
python -m pip install zstandard==0.25.0  # fixture decompression only
BRIDGE_TEST_CODEX=1 python -m unittest discover -s tests -p test_codex_native.py -v
```

### Evidence and remaining boundary

Native CI job `121270027226` on PR #7 ran **all 8 tests, zero skips**, including
fresh/resumed adversarial receipts and interruption. Earlier runs discovered
native goals and the still-advertised question tool; goals were disabled rather
than allowlisted. Exact native evidence, not feature-list booleans alone,
established the tested boundary (`unified_exec` still reports true even when
configured false). The public-entrypoint model/version guard change requires
another exact-head CI run before final acceptance; previous green evidence is
not silently attributed to the newer code.

The implementation executor cannot create nested native turns because its
outer sandbox masks the daemon directory; installed schema/initialize tests
pass locally, but native turns run on the normal CI runner. No sandbox controls
were weakened to evade that restriction. Ordinary local discovery skips those
explicit opt-in cases. Neither scripted inference nor generated schemas prove
live OpenAI availability, billing or production performance.

External user-run Codex MCP is a separate pre-existing workflow, unaffected by
these managed-server qualification restrictions.
