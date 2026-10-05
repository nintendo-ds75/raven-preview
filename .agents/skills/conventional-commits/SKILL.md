---
name: conventional-commits
description: Use when creating or updating Git commits or pull requests in Bridge; format their subject lines as Conventional Commit headers.
---

# Conventional commits and PR titles

Before creating a commit or PR, choose a header of the form `type(optional-scope): imperative summary`. Apply the same format to the PR title. Use lowercase types: `feat`, `fix`, `chore`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `style`, or `revert`. Keep the summary specific to the actual change and omit a trailing period. For a breaking change, append `!` before the colon and explain the impact in the body.

Examples: `feat: start the populated Docker stack with one command`; `fix(auth): reject expired login tokens`; `chore: update development dependencies`.

Inspect the intended diff before choosing the type. Do not rewrite already-published history solely to conform to this convention. Follow any stricter repository rule too.
