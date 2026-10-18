# Identity: who a person is, and which field each check trusts

People reach Raven four ways. Each lands in one table; every link between
them is by an exact identifier, never by a display name.

## Tables

| Table | Source | Linked to a person by |
| --- | --- | --- |
| `people` (`bridge/graph.py`) | the source of truth: admins, GitHub sign-in, Slack directory, invitations, task-link claims | its own `id`; unique `email`, `github_login`, `github_id`, `slack_id` |
| `owners` (`bridge/store.py`) + `decisions.owner_id` | routing and seeded owners | `owners.person_id`; an unbound row has only a name |
| `engineers`, `listings`, `gh_users` | git history, CODEOWNERS, GitHub | candidates only (`bridge/identity.py` matches name/email/handle); never authorize |
| `authority`, `team_members` | the authority map | `person_id` / `team_id` |
| `account_passwords`, `account_invites`, `api_tokens`, `brief_links` | logins, invitations, agent/human tokens, task links | `person_id` (`account_invites.person_id` names the invitee; email otherwise) |

## How the sources link

- **Ingest** (`bridge/ingest.py`, `bridge/owner_discovery.py`): git identities become `engineers` and owner rows; `graph.add_person(merge=True)` adopts an unbound owner row only when the name is unique.
- **Slack directory** (`bridge/slack_directory.py`): by `slack_id`, else exact email; a namesake gets a suffixed name and a new row. Replies resolve by `slack_id` (`bridge/slack_chat.py`, `delivery.slack_person`).
- **GitHub sign-in** (`bridge/auth.py`): `github_id`, else exact login, else a verified email of a person with no login yet.
- **Invitations** (`bridge/accounts.py`): exact email, or the person the admin names; name never.
- **Task links** (`bridge/briefing.py`): the link row's `person_id`; a session for another person refuses writes.

## Which field each authorization check uses

`bridge/authz.py` compares **person ids**: the owner row's `person_id`,
authority holders, team members, the coordinator setting. Two legacy
fallbacks still compare **exact names**: an unbound owner row and
`decisions.required_signers` (a JSON list of names), each resolved only
when exactly one person carries the name. Signatures (`decisions.signatures[].by`)
are names; `actor_id`/`actor_basis` carry the id and standing.

`/api/state` marks each decision with `can_act` from the same check; the
inbox counts those. `GET /api/decisions/:id` lists `allowed_actions`.

## Known gaps

- Routing carries candidates by display name (`bridge/signals.py`,
  `bridge/routing.py`), so an authority holder with a namesake can be
  routed to an unbound owner row (`tests/test_identity_paths.py`, expected failure).
- Required signers are stored as names; a `required_signer_ids` column
  would close the second name fallback.
- A task link with no session acts as its person wherever it is opened.
