# Claude customer loop, October 12, 2026

See [the report](../../../../reviews/main-129845b-claude-loop.md) for the outcome,
fixes, failed attempts and limits. This folder is evidence from a supervised live
run, not a deterministic benchmark score.

* `brief.txt`, `oracle.json`, `test_independent.py`, `boundary.json`: task and
  precommitted evaluation boundary. The task was synthetic on frozen urllib3.
* `mcp.jsonl`: requests and responses captured between the actual host and Raven,
  including hook and supervisor calls. Early discovery failures remain present.
  `gateway-capture.json` records excluded non-JSON proxy diagnostics.
* `claude-actions.jsonl`: assistant text, tool calls and tool results from the
  actual resumed session. Hidden reasoning and unrelated runtime metadata are
  omitted. It includes the original run and both resumptions.
* `slack-messages.json`, `connector-calls.json`, `fixture-state.json`: local
  provider fixtures and the actual outbound API payloads they received.
  `services.py` is the exact credential-free simulator (mount this folder as
  `/fixtures`, with `fixture-state.json` copied to `state.json` in a writable test
  directory). `mcp_proxy.py` is the repaired streaming capture proxy. Its Docker
  upstream name and fixture port identify this isolated run.
* `audit.jsonl`: evaluator actions, including each simulated human reply.
  `audit-head.json` pins the original private chain and the redacted public
  projection separately. The public chain was rebuilt after redaction.
* `tree-final.json`, `task-trace.json`, `proof-final.json`, `proof-check.json`:
  current state, provenance and saved proof. The final proof was fetched after
  the host saved it. Redacted exports are projections of the originals; an
  original integrity claim is not a new signature over a redacted document.
* `urllib3.patch`: the exact generated change. The source project's MIT license
  applies; see [urllib3](https://github.com/urllib3/urllib3).
* `supervisor-*.json`: the installed supervisor's real resume and acknowledgement
  receipts. No fake process runner was injected.
* `validation.json` and `*-tests.txt`: check outcomes and concise terminal output.

Credentials, accountless-link secrets, workstation paths and public-repository
identity names/emails are removed. Synthetic contacts are fictional. Credential
redaction does not make every source document universally safe to share; these
fixtures contain only controlled test data and public repository material.

To rerun the independent checks, apply `urllib3.patch` to a clean checkout of
`3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, install urllib3's test dependencies,
and run this folder's `test_independent.py` plus `test/test_retry.py` and
`test/test_poolmanager.py` with that checkout's `src` on `PYTHONPATH`.

To repeat the live experiment, create a fresh Raven Docker/PostgreSQL instance,
ingest only the sealed history, attach fixture Slack/Jira/docs services, install
the repository's Claude hooks and HTTP MCP connection, then give Claude only
`brief.txt`. Keep the oracle outside the host's mount. Respond through signed
Slack events and the read-back codes generated for that run. Run the installed
`watch_once` supervisor when Claude stops, so it resumes the same session. Do not
replace the supervisor with a scripted tool sequence or inject the decisions
directly into Claude's prompt. Live model outputs, IDs and timings will vary.
