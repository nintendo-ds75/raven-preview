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
are names; each entry written since the signer id was kept also carries
`person_id`, and `actor_id`/`actor_basis` carry the last actor's id and standing.

`/api/state` marks each decision with `can_act` from the same check; the
inbox counts those. `GET /api/decisions/:id` lists `allowed_actions`.

## How routing binds the owner

A ranked candidate is a display name that carries the person id of the
signal that ranked them (`identity.BoundName`): the authority row's
person, the one person a git address belongs to, the one person a
listing handle is the login of, a contact or coordinator record.
`Graph.owner_id_for` binds that id first and resolves the name only for
a candidate no signal could tie to a person (a git identity with no
person record), where the legacy unique-name rule still applies. A git
author who shares a map person's name but not their address carries no
id, lands on an unbound row, and nobody holds that row by the shared
name (`tests/test_identity_paths.py`).

The task page's "yours" marks (`signed_by_me`, `owed_by_me` on each
brief node) are computed server-side from ids; the viewer's name is
only used for display.

## Known gaps

- Required signers are stored as names (`canvas._approvers` has the
  ids but no column to keep them); a `required_signer_ids` column
  would close the second name fallback.
- Signals merge git history by normalized name (`signals._signal_route`
  keys candidates by `_norm_person`), so two namesakes' history scores
  as one candidate; the id attached is the authority row's when one
  applies, else only an address or login shared by no other person.
- `task.js` picks the default node to open by the signed-in name
  (`taskBriefDecision`); a wrong pick opens another node, never acts.
- A task link with no session acts as its person wherever it is opened.
