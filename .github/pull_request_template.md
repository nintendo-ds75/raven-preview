## What and why

<!-- One or two sentences. Link the issue, trace or eval that motivated it. -->

## How it was verified

- [ ] A test that fails without this change and passes with it (or: why none is possible)
- [ ] `python -m unittest discover -s tests -p 'test_*.py'` passes locally
- [ ] Routing changes: qemu routing bench and the case replays are not worse (paste the numbers)
- [ ] UI changes: the browser suites in `tests/browser*.cjs` were run by hand

## Review checklist

- [ ] Title follows `type(scope): summary` (conventional commits)
- [ ] Authorization, approval and signature semantics are unchanged, or the change says how and why
- [ ] No credential, customer data or evaluator answer key added to the repository
- [ ] Schema changes are additive and migrate existing databases (SQLite and PostgreSQL)
- [ ] Docs that describe the changed behaviour are updated
