# Context and host mechanism checks

See [the implementation and validation report](../../../docs/context-host-validation-2026-10-06.md).
`manifest.json` identifies the base revision, sealed urllib3 input and exact
Airweave API specification used for the HTTP fixtures. `checks.txt` records
commands and test summaries, including initial failures. Source permissions,
Slack and coding-process responses are simulated where stated. These are
mechanism regressions, not a new live-model benchmark.

Reproduce focused checks from the repository:

```sh
PYTHONPATH=tests:. python3 -m unittest test_context_connectors test_host_sessions test_context_memory test_source_observation test_brief -q
python3 -m evals.newdev.walkthrough --repo /path/to/a/frozen/checkout
node tests/browser-context-memory.cjs
```

Browser scripts need Playwright and Chromium. PostgreSQL tests use the isolated
schema runner in `tests/test_postgres.py`; opt in with `BRIDGE_TEST_POSTGRES=1`
and `DATABASE_URL`. Never point an evaluation at customer data.
