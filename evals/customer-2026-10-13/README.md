# Customer validation, 2026-10-13

This directory records this run only. Earlier reviews are not acceptance evidence for these changes.

## Exact source

- Original remote/default main: `ec5817d31d7a9be9fc8bdd2562b63806c6b11216`.
- Updated runtime: local commit `485b495` (includes `2920d4a` and `f32c456`). Publication was not confirmed at this checkpoint.
- `source-manifest.json`: SHA-256 `57e186a9686e03519c17fdea8fb5c4a9ee2373c89fd96a6bfa0c443f575a02b4`; hashes all 91 tracked runtime files under bridge/ and web/.
- Runtime patch against original main: SHA-256 `2e103ea768851578220f170f3743fec3a683acafb21e85dbdd9e4512213826f9` (171565 bytes).
- Current tests patch: SHA-256 `6c25f7f359bf62910b370551eb3124956d11caf3f2a84fe7712e64750c22a17b` (36243 bytes). Includes explicit opt-in legacy conversation fixtures and new PostgreSQL coverage.

## Actual infrastructure observations

The existing Daytona sandbox was started with its original 15-minute auto-stop setting. Testing uses a new `/root/raven-customer-20261013` checkout, isolated Compose project `raven_customer_20261013`, and fresh project volumes. Existing projects and data were not overwritten. The approved free-credit testing window is two hours; the sandbox must be stopped afterward, not deleted.

Docker server 28.3.3 runs. PostgreSQL 17-bookworm and the rebuilt updated Raven app are healthy. Runtime patch checksum was checked before applying, and all 91 runtime file hashes matched the manifest after transfer. Build used INSTALL_SEMANTIC=false and INSTALL_HOSTS=true. No semantic model weights or real provider credential were configured. The app uses BRIDGE_MODEL_API=none, authentication on, and a private Daytona preview. The login endpoint returned HTTP 200 with its configured public Host; an unrecognized localhost Host returned 403.

The standard fresh Compose bootstrap created isolated development login files; their contents were not retrieved. No real customer or employee was contacted. Existing Slack connector delivery tests are simulated protocol tests, not real Slack delivery.

## In progress / blocked at this checkpoint

- PostgreSQL 17 contract rerun completed: 1860 tests in 1370.597 seconds, OK (skipped=8). All eight skips require Node for the shipped brief submit handler; these paths have separate JavaScript checks, but were not exercised against PostgreSQL in this image. Log `/tmp/raven-current-postgres-v2.log` SHA-256: `0c26c93462875e06c94f45ac4b34c354d7f0e8b8226b428308652a72bca888b3`.
- Ordinary Daytona preview warning was accepted under the authorized test scope. Restoring changed the dashboard terminal preview hostname. Port-substituting that link did not open the app: official Daytona documentation identifies signed previews as port-bound, so this was not valid app-preview access and proves no Raven network defect. No app binding was expanded.
- User approved temporary isolated test-account creation. Secure password/setup credential entry remains pending; no real account credential was copied.
- Real Anthropic host/internal inference blocked on an approved runtime credential. No fallback provider call was represented as real inference.
- Screenshots of the actual updated requester/expert UI have not yet been captured. Infrastructure screenshots are retained privately, not published here.
- Final local regression is green: `python -m unittest discover -s tests` ran 2489 tests in 499.133 seconds, `OK (skipped=4)`, exit 0. Exact code/test tree is local `3fcc912`; the final seven legacy fixture corrections are included. Log SHA-256: `aaf8ecb9dbe101eb20c0c1126d13cfbc2521532efb9a9a5df72b82d9b9890954`. Published main is `f98547ae8f7cbfc1a7ddd6daeabecf6951c44b3c`; fetched git tree `4f0f39b29d08462fa3f5f59f062ef3000ba89cdb` exactly matches local `3fcc912`.
- Ruff and `git diff --check` passed. Static CSS palette/11 contrast pairs, safe login return route, and customer inquiry UI unit tests passed again after the final edits. These are not real-browser screenshots.
- The first PostgreSQL attempt was invalidated: an initial Docker copy nested test files rather than replacing them, and a stopped CLI left a child test process alive. Both stale-import processes were explicitly interrupted; the correct `tests/.` copy was verified and one fresh verbose run started. Only the fresh run may supply acceptance evidence. A later terminal websocket closure was reconnected; the actual test process continued.

## Recovery observation

At 23:23 UTC, the sandbox was already Archived after its retained 15-minute auto-stop. It was restored within the approved window to recover the completed PostgreSQL log. Docker needed a stale PID marker recovery; the earlier container image/volumes were unavailable to the restored default daemon and a new isolated project startup rebuilt the app. That restored app was observed running with authentication on and local login HTTP 200, but is not additional PostgreSQL or host acceptance evidence. User explicitly approved temporary VNC. The sandbox lacked all desktop prerequisites. Official Alpine packages for Xvfb, x11vnc, noVNC, XFCE, Chromium, dbus-x11, xfce4-terminal, xauth and xdotool installed successfully, but the dashboard Start VNC flow still returned to its initial control without a desktop or actionable error. No VNC session was observed. A separate synthetic visual fixture served on loopback 7446; it was never represented as authenticated acceptance and no browser screenshot was obtained. The customer stack retained authentication on at loopback 7445.

Official preview behavior: https://www.daytona.io/docs/en/preview/ . Official desktop workflow: https://www.daytona.io/docs/en/vnc-access/ .

At 23:39 UTC, the synthetic fixture container and isolated customer app/database containers were explicitly stopped and confirmed stopped. Process checks found no Xvfb, x11vnc, websockify or xfce4-session process. The existing sandbox stop was requested afterward; final lifecycle verification is recorded below. No sandbox, volume or project data was deleted.

Final lifecycle verified at 23:39:58 UTC: dashboard state **Stopped**, preview access Private, auto-stop 15m, auto-archive 1m, auto-delete/destroy disabled. VNC panel states "Sandbox is not running". No test service remains active.
