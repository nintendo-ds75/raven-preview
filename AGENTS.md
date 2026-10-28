# Repository contribution convention

Use the [conventional-commits skill](.agents/skills/conventional-commits/SKILL.md) for every new commit or pull request. Commit subjects and PR titles must use `type(optional-scope): imperative summary`, with a lowercase type from `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `style`, or `revert`. Breaking changes may use `!` before the colon. Do not rewrite published history to retrofit this convention.

## Raven

This team uses Raven (the `bridge` MCP server) to route the judgment calls in a task
to the person who owns them. At the start of every task, before reading or editing
anything, call `bridge_start_task`, then follow what Raven returns for the rest of
the task.
