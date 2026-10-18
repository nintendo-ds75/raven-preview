#!/bin/sh
# The web UI checks that run in plain Node, with no browser: the DOM-stubbed
# unit checks, the page-logic checks that call Python for their fixtures,
# and the audit viewer. `make check` and CI both run this file, so the list
# lives in one place and cannot drift between them.
#
# The Playwright suites (tests/browser*.cjs, tests/brief-browser.cjs) need
# Chromium and are run by hand before a release; see CONTRIBUTING.md.
set -e
cd "$(dirname "$0")/.."
for f in tests/*-unit.cjs tests/web-*.cjs tests/audit-viewer.cjs \
         tests/brief-followup-state.cjs tests/brief-source-state.cjs tests/brief-first-screen.cjs; do
  if [ -n "$GITHUB_ACTIONS" ]; then echo "::group::$f"; else echo "== $f"; fi
  node "$f"
  if [ -n "$GITHUB_ACTIONS" ]; then echo "::endgroup::"; fi
done
echo "web checks passed"
