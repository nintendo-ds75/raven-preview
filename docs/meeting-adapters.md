# Raven voice adapters — primary-source research (2026-10-25)

## Recommended implementation scope
Ship one concrete Vexa meeting-participant adapter plus normalized transcript import. Browser microphone notes are a separate input: they do not attend a meeting. STT models alone are not meeting bots. Recall is a viable hosted follow-on, not an advertised working integration until its signed webhook lifecycle is implemented/tested. Do not join any real meeting during development.

## Vexa: genuinely self-hostable meeting attendance
Apache-2.0 official repository: https://github.com/Vexa-ai/vexa . Docker Compose / Lite / Kubernetes; PostgreSQL, Redis, S3-compatible recording storage, separate CPU/GPU transcription unit or configured STT endpoint. Helm alone does not supply transcription. Bots still require meeting-platform network access; self-host does not mean disconnected operation. https://vexa.ai/self-host

Current REST contract: https://docs.vexa.ai/api/meetings
- Base configurable; local default http://localhost:18056, hosted https://api.cloud.vexa.ai. X-API-Key authentication.
- POST /bots: platform (google_meet, teams, zoom), native_meeting_id, bot_name, explicit transcribe_enabled/recording_enabled. Teams numeric IDs need passcode. Zoom requires full meeting_url. Exclude Jitsi: docs disclose live transcription unproven.
- GET /bots/status; GET /transcripts/{platform}/{native_meeting_id}; DELETE /bots/{platform}/{native_meeting_id} stops bot, not artifact deletion.
- DELETE /meetings/{meeting_id} removes terminal artifacts, retains lifecycle row; in-flight 409. Backup retention separate.
- Lifecycle: requested, joining, awaiting_admission, needs_help, active, stopping, completed, failed. Transcript segments include segment_id, speaker, text, start/end.
- Scheduling defaults auto_join:true! Raven must not schedule implicitly; use local plans only. Missing STT returns 503; duplicate active bot 409; concurrency currently may return 403 despite 429 contract.

Webhook alternative: https://docs.vexa.ai/webhooks
PUT /user/webhook with webhook_url, webhook_secret, webhook_events object; GET reads config. Events meeting.status_change, meeting.started, meeting.completed, bot.failed. Verify X-Webhook-Signature sha256=HMAC-SHA256(secret, timestamp + '.' + raw body), X-Webhook-Timestamp Unix seconds; constant-time compare and replay window. Legacy Authorization bearer is not a substitute for signature checks. Delivery history exposes event_id but docs explicitly say history store is broken, so empty history proves nothing. Durable retries claimed, exact retry schedule not established here. Use receiver-side durable dedupe and REST reconciliation; do not implement guessed event payloads.

## Recall: hosted actual participant alternative
Platforms explicitly documented: Zoom, Google Meet, Teams, Webex, Go-To Meeting beta. https://docs.recall.ai/docs/meeting-platforms
API key in Authorization header (Token prefix optional); regional base. https://docs.recall.ai/reference/authentication
POST /api/v1/bot/ creates bot with meeting_url, bot_name and recording_config. https://docs.recall.ai/reference/bot_create
GET /api/v1/bot/{id}/ returns bot/artifact state; production status polling explicitly forbidden: use bot-status webhooks. https://docs.recall.ai/reference/bot_retrieve
POST /api/v1/bot/{id}/leave_call/ stops participant. https://docs.recall.ai/reference/bot_leave_call_create
POST /api/v1/bot/{id}/delete_media/ deletes retained media, not proof local copies/backups vanished. https://docs.recall.ai/reference/bot_delete_media_create
Transcript configuration lives in recording_config.transcript.provider; realtime transcript.data events use recording_config.realtime_endpoints. transcript.done/failed are separate dashboard webhooks; artifact retrieval GET /api/v1/transcript/{TRANSCRIPT_ID}/. Low-latency streaming mode explicit; accuracy default can delay minutes. https://docs.recall.ai/docs/bot-real-time-transcription
Dashboard status events use Svix; retry intervals after failures: immediate, 5s, 5m, 30m, 2h, 5h, 10h, 10h. Acknowledge after durable enqueue; dedupe before effects. Real-time delivery differs and cannot be replayed after retries. https://docs.recall.ai/reference/webhooks-overview
Exact signature guide page returned no substantive body via browsing: https://docs.recall.ai/docs/verify-events . BLOCKER: do not ship custom guessed signature verification or claim production integration without verifying this contract and tenant setup. Hosted proprietary service, not open-source meeting infrastructure.

## Google Meet: transcript source, not attendance
Official artifact API supports authorized participant/owner reads, not a bot joining the call. GET https://meet.googleapis.com/v2/conferenceRecords/{conferenceRecord}/transcripts and /transcripts/{transcript}/entries, paginated OAuth-authorized user context. Transcript creation must have been enabled in Meet; API entry retention is 30 days, Drive transcript retention differs. https://developers.google.com/workspace/meet/api/guides/artifacts
This is a future source import adapter; initial version imports user-authorized exported text, retaining provider/installation/external ID/version. Never label imports as Raven attendance.

## Safety / reliability requirements (Raven design)
Separate explicit permissions to join, transcribe, retain audio, and disclose transcript to every workspace reader. Visible bot name and participant notice/admission; no covert capture, no automatic calendar joining. Recording notice is not itself proof of consent. Maintain operator attestation/audit, stop control, revocation and artifact-delete status. Persist mutation intent before remote request; timeout means outcome unknown, never automatic duplicate join. Polling should not infer 'completed' from absence of a running bot. Treat transcripts as untrusted evidence, never operational instructions. Canonical content belongs in existing Store.add_record/context_memory, not a second transcript corpus. Withdrawal blocks existing dependent reuse and retains private audit history; do not falsely promise physical erasure.

## Implemented Raven service contract
`meeting_records.migrate(db)` adds metadata and command ledger only. `create(store,data)` is local-only and returns a meeting. `list_meetings(store,repo)` returns `{meetings,capabilities}`. `get(store,id,repo)` is scope-bound. Admin server integration must own authorization for every mutation.

Creation: `{repo,provider: vexa|import,namespace,title,platform,native_meeting_id}`. Namespace labels the evidence installation; it does not grant isolated control of the configured Vexa account. Each creation has an occurrence_id (explicit caller idempotency key or new Raven UUID); repeated room URLs do not share consent or transcript identities. Supplying the same explicit occurrence ID is rejected, so callers retain the returned ID for retry. Overlapping unresolved room dispatches are blocked across every repo and namespace sharing the server Vexa configuration until reconciled. Do not reuse an installation namespace for another provider account.

`set_consent(store,id,repo,data)`: requires explicit boolean join/transcribe/record/notice_confirmed plus by. A fresh affirmative consent can reset a definitively rejected join, never an unknown dispatch or rejected stop/delete. Failed stops preserve the prior capture state. `command(...,action,options={})`: join/stop/delete_remote; join options can include passcode and Zoom meeting_url, which are not persisted. Stop acceptance is `stopping`, NOT proof of stopped recording; reconcile reads the exact provider record before permitting terminal artifact deletion. Remote IDs captured from command responses bind exact artifact deletion. Unknown dispatch stays blocked; a deliberate stop can return the remote ID and allow reconciliation. Do not infer failure/completion from absent running bot.

`import_transcript(...,data)`: body, source_version, workspace_shared:true, by. `sync_transcript(...,data)` fetches Vexa segments after the same explicit disclosure attestation. Both return `{meeting,source,sources,changed,manifest}` and store canonical generic/meeting records. `source` is the first chunk for backward compatibility; consumers must use `sources` for the full transcript. Imports support up to 1,000,000 rendered characters in deterministic 16,000-character chunks (at most 63), with at most 20,000 structured segments; larger payloads fail atomically, never truncate. Stable occurrence+chunk keys identify canonical records; segment IDs, speaker labels, relative start/end times and draft markers remain in text across chunks. Revisions preserve chunk IDs, withhold removed chunks and invalidate dependent evidence; restoring a removed chunk creates a new readable version, never unhides historical versions. `manifest` reports completeness, character/chunk counts and original source revision. Completeness defaults false for manual imports; provider sync marks complete only on explicit completed provider state, never failed. Draft/final segments remain evidence, not approved decisions.

`revoke(store,id,repo)` clears future consent, withholds all versions of every imported chunk, invalidates downstream evidence reuse, and blocks reimport. It does NOT remotely stop the bot or erase private history. Use explicit stop then reconcile then delete_remote for provider artifacts. Transcript text is not placed in the metadata list, command ledger, or error messages. Credentials live only in server environment `BRIDGE_VEXA_URL`, `VEXA_API_KEY`; redirects disabled and response size bounded to 8 MiB; HTTP manual import route likewise allows 8 MiB JSON before module character/segment bounds. No webhook receiver or automatic join scheduler runs: the signature verifier is tested as a primitive only. Optional consented background observation is described below. Recall and native Google OAuth import are research follow-ons, not working buttons.

Verification: isolated offline tests exercise canonical idempotent import, scope boundaries, explicit disclosure, ambiguous timeout/no duplicate join, stopping vs terminal deletion, transcript revocation, adapter request shape, HMAC tamper/replay rejection. No real meeting/provider call executed; deployment must separately validate a pinned Vexa version with operator-authorized synthetic meeting and participant consent.

Google entries exact auth/paging reference: https://developers.google.com/workspace/meet/api/reference/rest/v2/conferenceRecords.transcripts.entries/list . OAuth scopes meetings.space.readonly or meetings.space.created under https://www.googleapis.com/auth/. Empty GET body, pageSize default 10/max 100, pageToken → nextPageToken, transcriptEntries array. Continue until no nextPageToken; a partial first page is not a complete transcript.

### Occurrence isolation and post-withdrawal controls
Vexa transcript sync requires an exact remote occurrence ID and uses `/transcripts/by-id/{meeting_id}`, never the latest transcript for a recurring room. Returned ID must match. Remote deletion likewise targets the exact provider record. Native-room stop is refused for planned or terminal occurrences, preventing an old occurrence from stopping a later room session. No consent inherits to a new occurrence.

`operational_view(store, meeting)` deliberately exposes capture-control metadata separately from evidence, omitting source IDs/chunks/raw errors and redacting title after revocation. Its `remote_stop_required` is not a claim the provider is currently recording; it flags unresolved capture that needs attention. GET/mutation server projections use this view so the source sanitizer can withhold transcript evidence without hiding Stop/Reconcile controls.

Migration copies initial `meeting_records`/`meeting_commands` metadata into occurrence-aware tables without rewriting canonical source history, retaining old audit rows. Repeated migration is idempotent. Normal deployment calls `migrate` through Graph startup. SQLite fixture tests exercise this path; PostgreSQL live migration is not claimed unless separately run by the main integration suite.


### Consented background observation (Vexa only)
`meeting_worker.configure_sync(store,id,repo,{enabled:true,workspace_shared:true},actor)`
requires a current human administrator plus this occurrence's explicit join,
transcription, and participant-notice consent. Enabling does **not** join a meeting.
The UI must explain that transcripts will be fetched in the background and become
workspace-wide evidence. `{enabled:false}` cancels future reads/imports, not capture
and not earlier evidence; Revoke withdraws earlier evidence, while Stop controls
capture. Each remains separately visible and deliberate.

`meeting_worker.migrate(db)` adds an observation ledger. `Worker(store, allow_operator=not auth.enabled).start()`
resumes due observations after restart; `.close()` stops the local thread.
Polls are read-only exact-occurrence status and transcript requests, at most four
occurrences per tick, 60 seconds between normal observations and 300 after errors.
No participant dispatch, uncertain-write retry, automatic source approval or
external action occurs. There is no polling of Recall (its API forbids it).
A unique 90-second lease and configuration revision fence late workers. Current
administrator activity/role, consent, revocation, exact remote ID and meeting
state/update timestamp are rechecked in the source publication transaction.
Disable/revoke/demotion during a fetch discard its result. Stop racing a fetch
also discards that snapshot; later observations may retain the final transcript
under still-active disclosure consent. Empty/draft transcripts stay incomplete;
`complete` requires completed status in both status and transcript responses.
Failed capture retains incomplete evidence and stops observation, never claims
success. Confirmed remote lifecycle remains visible even if transcript retrieval
fails. Explicitly completed empty transcripts finish as `complete_empty` without
fabricating a source; an empty response conflicting with earlier evidence is
flagged `empty_conflicts_with_evidence` for operator review, not silently erased.
Persisted local-operator grants are rejected when authentication is enabled. Provider/config errors are generic retry state, never raw credentials.

Offline observer tests use fake Vexa, including expiry/restart, current admin
loss, withdrawal during I/O, mismatched IDs, and incomplete terminal capture.
These are protocol/consent fixtures, not a claim of live provider attendance.
