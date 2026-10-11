# Contributing to Raven

## Before you open a pull request

Run what CI runs:

```sh
python -m pip install ruff==0.7.4      # once
ruff check .                            # pyflakes rules, see ruff.toml
python -m unittest discover -s tests -p 'test_*.py'
for f in tests/*-unit.cjs tests/web-*.cjs tests/audit-viewer.cjs; do node "$f"; done
```

PostgreSQL contracts run in CI against a real server. To run them locally,
start PostgreSQL and set `BRIDGE_TEST_POSTGRES=1` and `DATABASE_URL`
(`make test-postgres` does this inside Docker).

The Playwright browser suites (`tests/browser*.cjs`, `tests/brief-*.cjs`)
need Chromium and are run by hand before a release or after UI changes.

## What CI checks

| Job | What it runs |
| --- | --- |
| Lint | `ruff check .`: undefined names, unused imports and variables, syntax errors |
| Tests (Python 3.11, 3.13) | The whole unittest suite |
| PostgreSQL | `tests/test_postgres.py` against PostgreSQL 17 |
| Web UI logic | The DOM-stubbed Node checks of the inbox, setup, interview and audit viewer |
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
