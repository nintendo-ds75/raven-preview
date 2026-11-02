# Parcelwise live demo

A preloaded Raven for showing the product live on your own machine. Parcelwise is a
made-up shipping software company: nine people, the
[parcelwise/parcelwise-app](https://github.com/parcelwise/parcelwise-app) repository, and
the state the v5 demo video ends in.

```sh
git clone https://github.com/nintendo-ds75/raven-preview.git && cd raven-preview
export ANTHROPIC_API_KEY=...        # or be signed in to the claude CLI
scripts/parcelwise_demo.sh          # add --voice for Raven's own voice (about 450 MB, once)
```

Open http://localhost:7333 and sign in as anyone at `@parcelwise.example` (maya, ana,
jordan, lena, marcus, priya, sam, theo, noor) with the password `parcelwise-demo`. A
private window lets two people be signed in at once, say Maya in one and Ana in the other.
`scripts/parcelwise_demo.sh --reset` puts everything back the way it started.

## What's already there

- **Voice intros** for eight people. Each quote on a person's page opens the transcript
  it came from.
- **Raven noticed**, next to Ask Raven: gaps worth building, people who should talk, and
  calls nobody has made, each tied to what people said.
- **The bulk-buy task** Maya ran with Claude Code. Its PR summary tab lists six decisions
  and who signed each one, with five live replies and one Raven cache hit. The pull request
  is [parcelwise-app#15](https://github.com/parcelwise/parcelwise-app/pull/15).
- **Noor Haddad** joined customer success and nobody has talked with Noor yet. Sign in
  as Noor and start the voice intro from Noor's page under People.

## Claude Code

The script clones the repository into `~/.raven-parcelwise-demo/parcelwise-app` and puts
Raven's MCP server, with an agent credential for Maya, in that clone's `.mcp.json`
(untracked, never committed), and installs Raven's host hooks there (`.raven/` and
`.claude/settings.local.json`, untracked too). The hooks register the first prompt as a
task before Claude Code starts working; without them the kickoff depends on the agent
calling `bridge_start_task` itself, which a headless run has skipped (see
[host adapters](host-adapters.md)). Run `claude` there and give it a task. Questions it can't
answer from the record go to the people who own them. Sign in as that person to answer,
in the browser or on a quick call.

## Notes

- With `ANTHROPIC_API_KEY` set, Ask Raven answers in seconds. Without it, Raven runs the
  signed-in `claude` CLI once per step and an answer takes one to three minutes, so set the
  key before a live demo.
- Without `--voice`, calls use the browser's own speech recognition and voice (Chrome
  works best). With `--voice`, Raven transcribes and speaks on this machine with Whisper
  and Kokoro, and needs `ffmpeg` on PATH.
- The demo binds to localhost only. The password and the accounts are for this synthetic
  workspace.
- The database is `fixtures/parcelwise-demo.db`. It was built from the recording's
  database by bridged `demo/raven-video/v5/tools/make_demo_snapshot.py`.

## For you

A signed-in person lands on **For you** (`#you`): one sentence written for them ("Raven
needs you for 1 thing. It handled 1 for you from calls you already made, without asking."),
then only what truly waits on them, with why it reached them instead of being settled
(no earlier call covers it; the closest earlier call may not carry over; they decide for
the area), Raven's best guess when it has one ("Raven would apply this, but isn't sure:
your call"), what the agent keeps doing meanwhile, and a way to answer in place or take a
quick call. Below that, "Handled without you": what Raven settled from this person's own
calls, each naming the call it used and why it fits, with the one-click correction. Then
"Your calls that changed". `GET /api/attention` (`bridge/attention.py`) scopes all of it
to the viewer and the tasks being built now. In the fixture: Priya has a guess to confirm
(the Bloom & Co overage question, routed through the agent's own path), Ana has one open
call and one handled from her Jun 2 call, Jordan sees his own correction, Maya sees the
calm empty state.

## Being built

The sidebar's **Being built** page (`#tasks`) is the judgment log: every live coding-agent
task and each judgment call the agent hit in it, as light tiles. The header and each task
say plainly how many calls came in, how many Raven settled from a call someone already made
without asking anyone, how many reached a person and who, and who was not asked because an
earlier call of theirs covered it. Clicking a tile shows the answer, why Raven applied the
precedent (one line), the earlier call itself (who, when, how their sign-off arrived, the
quote, a link), the records the agent read, and one action for the owner: **That's wrong,
or doesn't apply here**. The correction is recorded under the person's name, retires the
earlier call (shown as replaced) and becomes the precedent the next similar question uses
(`bridge/judgment.py`, `POST /api/decisions/<id>/correct`).

Under the counts, each card carries **What's been done**: the task's story as a timeline,
one plain sentence per row with the clock beside it ("Engaged: Alex's prompt read; 14 judgment
calls identified", "Settled 4 from earlier calls, nobody asked: …", "Flagged 2 to Dana, who
decides for pricing decisions: …", "Raven's guess on … from Dana's Oct 29 call; Noah confirms",
"Dana handed … to Elena", "Raj corrected …"). Every row is a stored event rendered by
`bridge/timeline.py` (`GET /api/tasks/<id>/timeline`, and `timeline` on each live task in
`/api/state`); nothing is composed by a model. The card shows the last two rows with **Show all**,
so the open calls below keep the eye; the task page shows the whole story above its decisions.
An earlier call or a flagged question in a sentence opens it, a record shows as a pill.

The fixture has two live tasks, both picked up by Maya Chen: the bulk label purchase from
the recording, where Jordan has already corrected one call Raven settled from his Oct 28
answer, and **Turn on overage billing for the Starter plan**, seven decisions with three
still waiting on Lena, Sam and Ana. `scripts/seed_billing_task.py` adds that task to a
database, `scripts/seed_judgment_demo.py` adds the channels, fit lines and Jordan's
correction, and `--reset` moves both live tasks' times up to now. To show the loop live,
sign in as ana@parcelwise.example, open "Must a Starter merchant see the overage price…"
and correct it.
