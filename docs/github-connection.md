# Connect GitHub

Users connect through a registered GitHub App: choose repositories, then approve
a one-time code on GitHub. Raven stores their user access and refresh tokens in
its private data volume. It needs no hosted token service, app private key, or
client secret. Device authorization is an extra GitHub step, not a one-click popup.

Raven includes the public identifiers for the registered
[Bridge Repository Access app](https://github.com/apps/bridge-repository-access).
Run `./setup` and choose **Connect GitHub** in the browser that opens. No environment
configuration or app registration is needed for users.

## Optional: use your own app

Fork maintainers or operators who want a separate integration can override both
public identifiers. Existing operator-owned app credentials are preserved when
no override is configured.

1. Open https://github.com/settings/apps/new under the account or organization
   that will own the Raven integration.
2. Set a unique app name and the project's homepage URL.
3. Enable **Device Flow**. Keep user-token expiration enabled. Leave webhooks
   disabled. Leave the callback and setup URLs empty; this flow uses polling.
4. Request repository **Contents: read-only**, **Pull requests: read-only**,
   and organization **Members: read-only** for team ownership evidence.
   Organization permissions can require organization-owner approval.
5. Allow installation on **Any account**, then create the app.
6. Copy its public **Client ID** (not App ID) and its slug from the app's URL
   into the deployment's `.env`:

   ```dotenv
   BRIDGE_GITHUB_APP_CLIENT_ID=Iv1.your_public_client_id
   BRIDGE_GITHUB_APP_SLUG=your-bridge-app-slug
   ```

7. Run `./setup`, or open **Connections & setup** in the running app.

These two identifiers are public. Never distribute an app private key or client
secret. Raven's device flow does not use either.

## Authorization and credential storage

Users first select repositories on GitHub, return to Raven, and click
**Authorize connection**. They enter the displayed code at GitHub and approve.
Raven refreshes expiring tokens automatically and syncs selected repositories.
After changing the app's selected repositories, use **Refresh connected
repositories**. GitHub enforces access revocation even before that refresh.
Closing the authorization dialog stops browser polling; pending codes expire.
Revoked or expired refresh tokens require reconnection.

The `github-user.json` file in Raven's data volume contains credentials, is
written with owner-only permissions, and must be protected in backups. Existing
operator-owned GitHub App installations remain supported when the shared app
identifiers are not configured.

References: [GitHub App user authorization](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app)
and [refreshing user tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/refreshing-user-access-tokens).
