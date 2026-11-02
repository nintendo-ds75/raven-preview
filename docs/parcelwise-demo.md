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

The sidebar's **Being built** page (`#tasks`) shows every live coding-agent task and each
judgement call in it: whether Raven settled it from strong precedent, had a close precedent
a person confirmed, or needed a novel judgement; who Raven talked to and whether they have
answered; and where the context came from (the earlier decision, ADRs, pull requests, Slack,
Jira, files). The fixture has two live tasks, both picked up by Maya Chen: the bulk label
purchase from the recording, and **Turn on overage billing for the Starter plan**, seven
decisions with three still waiting on Lena, Sam and Ana. `scripts/seed_billing_task.py`
adds that task to a database; `--reset` moves both live tasks' times up to now.
