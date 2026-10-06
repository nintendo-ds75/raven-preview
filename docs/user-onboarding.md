# Workspace accounts and repeatable verification

The default authenticated browser setup opens **Create your workspace**. Step 1 asks only for the workspace
name and authorization to claim the deployment. Step 2, **Create your profile**,
asks for your name, email and personal password (12–256 characters). The first
profile automatically becomes admin. No password belongs to the workspace.
When this browser flow is started, normal pages, HTTP APIs, HTTP MCP and webhooks remain blocked until both steps finish.
Direct database tools and local stdio MCP are operator-level access, not web login boundaries.
Existing completed workspaces remain ready without re-onboarding. Claiming the workspace
requires an existing admin session or setup credential; it is never first-visitor
wins. For local Docker installs, `./dev login` supplies that one-time bootstrap
credential. A deployment may instead provision `BRIDGE_ADMIN_TOKEN` securely.

The two steps are linked by a private, one-hour setup cookie; only the original
creator can finish the profile. If it expires, re-enter that creator's setup
credential. A restart preserves the pending workspace. Authentication must be
enabled for this browser account-creation flow.

Operators can instead initialize an accountless workspace with
`./setup --workspace "Your team" --configure`. This headless path keeps the
operator credential separate from the limited MCP credential and does not
require a personal password. A recipient can answer in Slack without completing
either browser step. A direct Python server with authentication off is a trusted
loopback-only operator installation, not shared workspace authentication.

## Model

- **Workspace:** the single shared Raven deployment and its data.
- **Users:** individual profiles and login credentials; the first profile is admin.
- **Owners:** people designated to own decisions, paths or repositories, not a separate login system.
- **Agents:** limited credentials acting on behalf of users.
- **Invites:** admin-issued access to this workspace.

No workspace switching or multi-tenant isolation is introduced. Deploy a separate
instance and database for each organization; viewer and member accounts can read
workspace data rather than only the tasks assigned to them.

## Accountless task links

Raven can include a personal task link in a Slack DM. It opens one task without
a browser account and permits actions on the recipient's existing standing.
It is a bearer credential: anyone holding it can act as that person on that task.
Do not forward it. The link expires after 14 days by default, can be revoked,
and stops working when the recipient is made inactive. It never grants an
administrator override or access to unrelated tasks. Public fallback messages
do not carry recipient links.

An eligible recipient can optionally create a personal login from their task
page; this is separate from answering in Slack. Administrators use the normal
sign-in or invitation flow. An operator can disable task-page signup with
`brief_signup=0`. See [the task-page contract](reference.md#the-task-page-a-message-links-to).

## TODO: secure workspace sharing

- [ ] Add a dedicated Share workspace panel for managing/revoking invitation links.
- [ ] Offer a short-lived join code as an alternative to private invitation links.
- [ ] Add join-code attempt limits, expiry, single-use redemption and audit events.
- [ ] Let admins choose recipient restrictions and access role; never make a shared link public admin access.

The existing single-use, email-assigned invitation links remain available; this
TODO covers the fuller sharing/code experience, not unrestricted public signup.

Existing accounts and data are preserved. An existing admin can complete setup
from Connections or People & ownership; their identity remains the same. The
Docker development-login credential is revoked on completion and is not recreated
on restart. Existing agent credentials are preserved. Keep an operator bootstrap
credential in a secret store if you need a recovery path: password reset email is
not implemented yet.

Admins select **Invite teammate** from Connections or People & ownership. Choose
viewer or member access, then copy and privately share the link. Links expire in
48 hours, can be redeemed once, and are replaced by creating another invitation
for the same email. There is no automatic email delivery. Possession of the link
is the invitation proof; this is not independent email verification. Existing
accounts cannot be overwritten through invitations. Invitations do not grant
admin access. Owners and invitees land on Connections to configure repositories
and agents; returning users can sign in with email/password. GitHub sign-in and
human-token login remain available.

Passwords use salted scrypt hashes. Invitation tokens are stored hashed. Password
login is limited to ten attempts per account in a 15-minute window, persisted in
the database. Shared deployments need HTTPS and should enforce an additional
network-level rate limit. Sessions retain Raven's existing 14-day lifetime.

## Automated tests (isolated data; no real invitations)

```sh
PYTHONPATH=tests:. python3 -m unittest test_accounts -v
python3 -m unittest discover -s tests -q
node tests/web-setup.cjs
```

To exercise PostgreSQL contracts, including account HTTP flows, using temporary
schemas in the configured Docker database:

```sh
docker compose run --rm -T --entrypoint sh app -c 'BRIDGE_TEST_POSTGRES=1 PYTHONPATH=tests:. python -m unittest test_postgres -q'
```

The PostgreSQL harness creates uniquely named test schemas and removes only those
schemas. It never resets the live workspace. Tests cover owner claim, invite
acceptance/replay/expiry/replacement, returning login, role boundaries, inactive
accounts, CSRF/origin rejection, hashed secrets, and development-login migration.
