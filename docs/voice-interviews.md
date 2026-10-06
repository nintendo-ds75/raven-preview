# Attributed voice and text interviews

Raven can interview an authorized decision-maker before recording a decision. Open a decision in the inbox, or the personal task page linked from its Slack message, and select **Start or resume interview**. Browser dictation and spoken prompts/readback are optional; typing works without them.

## What the workflow does

1. Binds a private draft to one person, task, decision, repository/path scope and reviewed decision revision.
2. Offers scoped questions about the proposed answer, boundaries, exceptions, implementation constraints and reasoning. Each response, its prompt and any pending response are persisted.
3. When the existing model backend is enabled, sends the saved task context and responses through Raven's model client to propose a targeted next question, a readback and caveats. The person can copy the proposal into editable answer fields.
4. Saves the exact answer, rationale and optional structured reuse boundaries, then shows a separate review screen. **Read decision aloud** reads that exact saved content.
5. Only **Confirm and sign as …** records the answer. The ordinary live authority and stale-revision checks run again. The interview confirmation, provenance and signed decision commit in one database transaction. Other required signers remain required.

A transcript, model proposal, spoken readback, saved draft or microphone result never authorizes code. The feature does not make a standing rule, mark code verified or claim that a voice belongs to a particular human.

## Adaptive and providerless modes

The adaptive interviewer uses `bridge.llm.Client` and the existing `BRIDGE_MODEL_API`, `BRIDGE_MODEL`, `BRIDGE_FAST_MODEL`, `BRIDGE_SEMANTIC` and provider configuration. It can use the configured Anthropic API or Claude CLI backend. No separate speech/LLM credentials are embedded in this feature.

Model output must have a strict JSON shape. Quotes grounding the follow-up and readback must occur in the supplied context/responses; caveats carry exact response quotes. Question-only clarification leaves both proposed answer and proposed rationale empty while retaining grounded caveats. A schema or grounding failure gets at most one repair attempt using the original human evidence and a fixed validation code; rejected text is not promoted into a source. Persistent invalid output, failed inference and missing providers yield an explicitly labelled guided fallback. A valid model proposal remains unapproved and can be wrong; the human must review every condition and caveat. The server ignores any replacement answer sent directly to the confirmation endpoint: it signs only the saved version shown for review.

Without a backend, or with `BRIDGE_MODEL_API=none` / `BRIDGE_SEMANTIC=0`, Raven supplies deterministic guided questions. It does not pretend to conduct an AI conversation or invent a readback. The person writes the final decision. Adaptive turns are bounded to ten; transcripts, responses and request sizes are bounded rather than silently truncated.

## Identity, scope and task links

- Workspace interviews require an active member/admin with a session or human token. Agent tokens, local operator/bootstrap identities and viewers cannot confirm an attributed interview.
- A personal `/brief` link uses the existing `X-Raven-Link` credential. Its actor and task come from the server-resolved link, never client fields. A decision-scoped link may access only that decision's interviews. It does not create an account, session cookie or workspace-wide API access.
- Link validity, expiry, revocation and current person status are checked again inside writes, after model inference and during atomic confirmation. A revoked link cannot complete an in-flight proposal or sign a decision.
- Link possession represents the recipient's scoped identity, as it does for existing task-page answers. A forwarded link can be used by its holder. This is attributed task-link confirmation, not biometric speaker verification. Keep links private.
- Assigned-owner, required-signer or verified scope authority is still required. The interview does not grant administrative override or silently take ownership of an unassigned decision. Assign its owner first.
- Abandoned tasks and withdrawn decisions cannot receive new interview decisions. Existing private drafts remain readable and can be cancelled. A private unconfirmed draft does not prevent task abandonment.

## Privacy and browser behavior

The microphone starts only after **Start microphone** and the browser's permission flow. The UI warns beforehand that browser recognition may send audio to the browser provider. Availability depends on browser support and a secure context, normally HTTPS or localhost. Raven does not request microphone access automatically and stores no audio.

Saved text and task context are sent to the configured model provider when adaptive follow-ups are requested. Speech synthesis also uses the browser/OS speech service; voice processing may depend on the chosen system/browser voice. Do not dictate content you are not permitted to send through those services. Use typed, providerless mode when required.

Draft interview records are accessible through the API only to their person within the applicable task/link scope. Workspace/task history records the start, failure/cancellation and confirmation metadata, not raw draft transcripts. The signed answer and its rationale become ordinary decision data visible to the task and agent. Database operators can access stored records.

**Discard this interview** cancels it without recording an answer. It does not erase previously saved transcript data from the database. There is no special audio-retention service, transcription vendor upload endpoint or automatic deletion policy.

## Interruptions and recovery

- Draft, prompt/response turns, pending response, failure state and confirmation survive a server restart in the shared database.
- Save, speech-result persistence and close writes are serialized against an optimistic interview version. Stale edits are refused.
- Escape/Close saves the current draft and stops microphone capture and speech output. Navigation stops audio and attempts the same save. A network or validation failure is shown; it is not reported as saved.
- Page exit stops microphone and speech output but does not guarantee a final network save. Save explicitly before closing the tab; only acknowledged drafts are durable.
- Discard, switching from speech to review and confirmation stop audio. Late recognition events are ignored after capture stops.
- A delayed model response cannot redraw over newer unsaved edits. A cancellation or newer saved version invalidates model output before it is committed.
- Repeating the same acknowledged confirmation is idempotent. If the decision changed elsewhere, start a new interview against the new revision instead of signing the stale draft.

## Verification

From the repository root:

```sh
python -m unittest discover -s tests -p test_interview.py -q
node --check web/interview.js
node --check web/brief.js
node tests/interview-ui-unit.cjs
node tests/browser-interview.cjs
```

The Python suite covers attribution, raw drafts not signing, restart/retry, atomic rollback, stale/conflicting writes, task/person/decision boundaries, current authority, abandonment, cancellation, strict model validation, explicit fallback, task-link expiry/revocation and HTTP authentication/CSRF. Provider inference is mocked in model contract tests.

The UI unit runner uses DOM and audio stubs; it checks both entry surfaces, escaping, delayed response/edit guards, stale launches and audio cancellation. It is not a real browser check.

The browser runner uses an isolated temporary server/database and mocked speech APIs. It is designed to exercise the actual inbox/task-link UI, explicit confirmation, resume, recognition failure, unsupported browsers, Escape/navigation/discard and spoken readback. Set `BRIDGE_BROWSER` if Chromium is installed elsewhere. The initial restricted build environment could not run Chromium's socket sandbox. The suite was subsequently run successfully in Chromium inside a separate Docker-capable test sandbox, including a rerun on runtime `764b1c4`; speech APIs remained mocked. See the dated verification report for source checkpoints.

A bounded live Anthropic acceptance run verified adaptive text follow-up and caveat extraction alongside the synthetic Slack/MCP workflow. Actual microphone permission/recognition, audible speech and native Slack huddles remain unverified. Automated mobile viewport checks do not establish microphone compatibility on physical mobile devices. Raven opens a browser interview from Slack; it does not join or record a Slack huddle. See [verification evidence](verification-2026-10-06.md).
