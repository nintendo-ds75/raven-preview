# Contributing to Raven

## Before you open a pull request

Run what CI runs:

```sh
python -m pip install ruff==0.7.4 -r requirements-teams.txt   # once; PyJWT for the Teams tests
ruff check .                            # pyflakes rules, see ruff.toml
python -m unittest discover -s tests -p 'test_*.py'
./scripts/check_web.sh                  # the Node UI checks, no browser
```

`make check` runs the same three steps. `python3 scripts/test_timing.py`
times the Python suite file by file and lists the slowest tests.

PostgreSQL contracts run in CI against a real server. To run them locally,
start PostgreSQL and set `BRIDGE_TEST_POSTGRES=1` and `DATABASE_URL`
(`make test-postgres` does this inside Docker).

The Playwright browser suites (`tests/browser*.cjs`, `tests/brief-browser.cjs`)
need Chromium and are run by hand before a release or after UI changes.

`tests/browser-expert-inbox.cjs` explicitly keeps the Chromium sandbox enabled.
Run it as a non-root user in an environment that supports Chromium sandboxing,
with a writable checkout, temporary directory and artifact destination. A root
container or a runtime that blocks the required process/socket facilities is an
environment blocker, not a failing application assertion. Use a suitable
non-root desktop/container session; do not disable the sandbox to make it pass.

## What CI checks

| Job | What it runs |
| --- | --- |
| Lint | `ruff check .`: undefined names, unused imports and variables, syntax errors |
| Tests (Python 3.11, 3.13) | The whole unittest suite |
| PostgreSQL | `tests/test_postgres.py` against PostgreSQL 17 |
| Web UI logic | The Node checks in `scripts/check_web.sh`: the DOM-stubbed UI units, the page-logic checks and the audit viewer |
| Conventional commits | Pull request titles and new commit headers |

## Writing a change

- **Commits and PR titles** follow conventional commits:
  `type(scope): imperative summary`. See `AGENTS.md`.
- **Every bug fix comes with a test that fails without the fix.** Name the
  test after the behaviour, not the bug number.
- **Routing changes** are measured on the qemu routing bench
  (`python -m bench.routing.replay --datasets qemu --conditions warm,pathless`)
  and must not lower top-1 or raise the wrong-person rate. Include the numbers
  in the pull request.
- **Keep pull requests small.** One behaviour per pull request is easier to
  review and to revert.
- **Tests take their stores from `tests/fixtures.py`:** `self.fresh_store(name)`
  for an empty one and `self.warm_store(fixture)` for one with a fixture
  repository ingested. Both copy a template made once per process; creating
  the schema or ingesting again per test is what made the suite slow.
- **Schema changes are additive** and must work on SQLite and PostgreSQL.
- **Never commit** credentials, customer data, `.env` files, or evaluator
  answer keys.

## Reviewing

Both maintainers own the whole repository (`.github/CODEOWNERS`). A reviewer
checks, in order:

1. **Correctness:** does the test prove the behaviour, and would it fail
   without the change?
2. **Authority:** does anything change who may answer, sign or approve, or
   what counts as authorization? Such changes need an explicit note.
3. **Data:** are migrations additive and safe on existing databases?
4. **Clarity:** is the code as simple as the behaviour allows, with comments
   that say why, and are the docs updated?

Recommended repository settings (GitHub, Settings → Branches): protect
`main`, require a pull request with one approval from a code owner, require
the CI and conventional-commit checks to pass, and disallow force pushes.
