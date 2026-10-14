# Customer browser validation, 2026-10-14

## Actual browser environment and source

The dot cloud computer's Chromium rendered Raven's real Python server and SQLite database at a loopback URL. Source was downloaded using GitHub's official Download ZIP flow because direct native-terminal git network access failed. The latest visually inspected archive was main `8529430b6d188c3f043677181549ec7583dc5138`; the GitHub commit was checked before download. No Daytona sandbox was restarted for this browser pass.

The server was started with `python3 -m bridge --demo --port 7446`. Data is Raven's synthetic illustrative seed. This was a local-operator fixture, **not authenticated expert onboarding, live Claude execution, or real Slack delivery**. No provider requests or real employee communications occurred during that visual pass. Screenshots of the actual rendered task page and inbox were delivered privately; they are not published in this repository.

## Tested interactions and findings

- New-conversation screen reports missing context/execution setup and does not pretend a task can run.
- Tasks list opens the selected task. Overview shows current blocker, selected human owner, next action, and an unconfirmed prior-decision suggestion separately from authorization.
- The expert inbox explicitly labels its sample workspace; it distinguishes new judgment, prior-decision prediction and agent-settled evidence awaiting signoff.
- Expanding a task decision reveals rationale, evidence/answer origin and routing on demand. Opening its decision modal exposes context, alternatives, required human signoff and separate read-only clarification.
- Both decision-modal and task-page inquiry returned deterministic recorded evidence when inference was unavailable. They disclosed that an evidence reading creates no decision, signature or authorization. Visible approval state remained unchanged.
- Closing the modal restored keyboard focus to its Open decision trigger. Back returned to the task route. New sidebar navigation was retested after the scroll-reset fix and opened the inbox at the top.
- Real rendering caught a blank white logo caused by an SVG-flattening CSS filter; the fix was visually confirmed.
- Real rendering caught the absence of a task-level inquiry composer despite backend support; it is now on the primary task page.
- Real rendering then caught an unstyled inline textarea. Full-width two-row styling and compact status presentation were visually confirmed. Redundant lifecycle details moved into a disclosure while current owner/blocker/next action remained visible.
- Submission originally dropped keyboard focus when disabling/replacing the Ask button. The guarded aria-disabled/stable-ID fix was retested in actual Chromium from published `bb58e3c34fe72c6ba4fe97bcbcd258f31dea48f7`: task submission retained focus on `task-inquiry-submit`, and modal submission retained focus on its Ask Raven button.

## Verification boundaries

Fourteen JavaScript suites passed after the layout/focus changes, using parsed DOM/API doubles where identified by those suites. Earlier backend evidence remains 2489 Python tests (4 skipped) and 1860 PostgreSQL tests (8 skipped); those runs preceded these frontend-only follow-ups. They are not substitutes for the browser observations above.

The observed browser viewport was 1180 CSS pixels wide. A native-window resize attempt did not change that measured width, so mobile/narrow-viewport acceptance is **not claimed**.

The current cloud-browser computer had no Claude executable on PATH and no ANTHROPIC_API_KEY environment value (presence only was checked). This does not establish whether a different existing sandbox has an authorized provider runtime. No stored credential was read or copied. Live host wait/resume, provider-grounded synthesis, and authenticated fresh-expert onboarding remain outstanding.

At 00:30 UTC, the separately named existing Daytona inference sandbox was inspected in place. It has a Claude executable, the Claude Agent SDK, and a provider environment credential present. Only presence booleans and version metadata were queried, never credential values. This corrects any inference that all existing sandboxes lacked provider configuration; provider validity and live acceptance are still unverified.

## Bounded provider check

The existing inference sandbox was started at approximately 00:28 UTC and checked using its already configured Anthropic client environment. One request asked `claude-sonnet-4-6` to return a fixed smoke-test marker, with a 32-token output limit, 30-second timeout and retries disabled. It returned `AuthenticationError`, HTTP 401. There was no successful response, returned model identity or metered token-usage object. No additional provider calls were attempted. Credential values were never read, exported or copied. This is an authentication blocker, not a completed Claude-host/MCP acceptance run.

The dashboard showed this sandbox **Stopped** at 00:35 UTC. Its existing data was preserved; no VNC session was started in this sandbox. A valid credential must be configured through a secure user-controlled flow before the remaining live-host checks can proceed.
