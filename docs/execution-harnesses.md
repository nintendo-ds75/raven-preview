# Execution harness coverage

These are execution harnesses, not aliases for Raven's internal model calls.
Enabled Claude, Pi and OpenCode adapters use Raven's durable task, approval,
file/tool and uncertain-intent boundaries. Managed Codex is disabled: its native
built-in tools have not been contained by the attempted suppression flags.
No adapter adds permission to publish, deploy, or approve its own output.

| Harness | Integration | Verification in this change |
| --- | --- | --- |
| Claude Agent SDK 0.2.165 | Existing in-process Raven MCP tools; native tools denied | Existing offline contracts; no new live call |
| Codex app-server 0.162.0-alpha.17.2 | **Disabled in production.** Research transport only; no operator override | Fake protocol + installed native schema checks; full native turn blocked locally (see below) |
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
only through Raven's separately configured command sandbox. The disabled Codex research transport has no demonstrated parent-only read/network boundary. A provider-reported dollar amount is not a billing
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

The official npm pin `@openai/codex@0.162.0-alpha.17.2` is available and now has
an optional lockfile under `hosts/codex`. Run `npm ci --prefix hosts/codex
--ignore-scripts`; Raven prefers that package after an explicit
`RAVEN_CODEX_BIN` override and before a different system CLI. The optional
harness Docker image includes this pin too.

The installed pin's actual experimental JSON schemas were generated and checked.
Both start and resume responses require effective `model` and `modelProvider`;
Raven now rejects either identity changing before it starts a turn. The actual
resume schema has **no** `allowProviderModelFallback` request field, so Raven
checks the returned identity rather than sending an invented option.

The opt-in `tests/test_codex_native.py` exercises the real worker/app-server
through a test-only driver (not production dispatch) against a loopback scripted
Responses SSE provider, including native tool IDs,
parent tool results, and persisted session resume. A separate adversarial case
attempts native shell reads of synthetic private files/keys, native image reads,
and a loopback network exfiltration sink on both fresh and resumed sessions; it
also requires the advertised tool set to remain Raven-only after resume. It uses
a temporary HOME, dummy auth and the unchanged read-only/denied-tool controls.
Run on a host where the native sandbox can initialize:

```sh
npm ci --prefix hosts/codex --ignore-scripts
python -m pip install zstandard==0.25.0  # fixture request decompression only
BRIDGE_TEST_CODEX=1 python -m unittest discover -s tests -p test_codex_native.py -v
```

**Verification boundary:** installed-version and native-schema tests passed in
the implementation executor. The actual turn attempt was blocked before
inference: the surrounding Codex sandbox masks its shared daemon directory,
which the nested native sandbox requires to be user-owned mode 0700. A sanctioned
outside-sandbox test attempt was also blocked by executor setup before the test
started. No permissions, mounts or sandbox controls were changed. This matches
[upstream nested-sandbox issue #53799](https://github.com/openai/codex/issues/53799).
The full native loop is therefore **not locally passed**; the explicit
`codex-native` CI job must establish it on a suitable runner. Ordinary test
discovery skips the opt-in loop, and neither fake-protocol tests nor schema
validation are described as full native acceptance or live OpenAI inference.


### Codex containment finding

The exact pinned CLI's feature listing retains native capabilities despite
attempted `false` overrides. Denying app-server permission requests does not
intercept native tools that run without requesting permission. A read-only
sandbox can still allow reading and transmitting server-side data. Therefore
`capability('codex')` always reports unavailable and the production worker
unconditionally refuses Codex dispatch. Existing saved sessions are retained,
not silently routed to another harness. There is no environment opt-in.

External user-run Codex MCP integration is a separate existing workflow; this
restriction concerns starting Codex inside the Raven server. Re-enablement
requires demonstrated native built-in read/shell/network denial and the same
proof after session resume, not merely an advertised tool list or schema pass.

The native research CI gate is intentionally strict: it must fail on any builtin
file/key/image disclosure, loopback exfiltration, unexpected tool availability
or resumed-session regression. A passing version/schema check does not satisfy
that gate. These negative native-turn fixtures are locally **unrun** because of
the executor sandbox blocker; production remains disabled regardless of CI
configuration until an explicit reviewed implementation changes that policy.

Independent exact-pin probing confirmed `unified_exec` still reports `true`
when explicitly set to `false`. Explicit `view_image`, `skill_search` and
`skill_mcp_dependency_install` false overrides do report false and are now also
set in the research transport. These narrower denials do not resolve the
unified-execution boundary and do not re-enable managed Codex.
