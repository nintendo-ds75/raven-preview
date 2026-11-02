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
(untracked, never committed). Run `claude` there and give it a task. Questions it can't
answer from the record go to the people who own them. Sign in as that person to answer,
in the browser or on a quick call.

## Notes

- Without `--voice`, calls use the browser's own speech recognition and voice (Chrome
  works best). With `--voice`, Raven transcribes and speaks on this machine with Whisper
  and Kokoro, and needs `ffmpeg` on PATH.
- The demo binds to localhost only. The password and the accounts are for this synthetic
  workspace.
- The database is `fixtures/parcelwise-demo.db`. It was built from the recording's
  database by bridged `demo/raven-video/v5/tools/make_demo_snapshot.py`.

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

The fixture has two live tasks, both picked up by Maya Chen: the bulk label purchase from
the recording, where Jordan has already corrected one call Raven settled from his Oct 28
answer, and **Turn on overage billing for the Starter plan**, seven decisions with three
still waiting on Lena, Sam and Ana. `scripts/seed_billing_task.py` adds that task to a
database, `scripts/seed_judgment_demo.py` adds the channels, fit lines and Jordan's
correction, and `--reset` moves both live tasks' times up to now. To show the loop live,
sign in as ana@parcelwise.example, open "Must a Starter merchant see the overage price…"
and correct it.
