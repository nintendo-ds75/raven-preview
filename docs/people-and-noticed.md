# People and "Raven noticed"

## People (`#people`, `#people/<id>`)

The map is gone. Drawing people against area words didn't say much. People
is a search box and a page per person:

- **Decides / Knows about / Has been writing**: the authority map and git.
- **What Raven has learned**, newest first: what they told Raven (voice
  intro, notes they or an admin added, things they told an agent that the
  agent kept with `bridge_remember`) and the decisions they answered for
  agents, each with where and when.
- The person, or an admin, can add, reword or remove a line. A reworded
  line keeps the quote it came from.

Search matches names, teams, areas and anything a person said or decided.

APIs: `GET /api/people/directory?q=`, `GET /api/people/<id>/profile`,
`POST /api/people/<id>/context` with `{"action": "add"|"edit"|"remove", ...}`.

What people told Raven also answers questions: Ask Raven
(`POST /api/ask`) and `bridge_ask_raven` read these lines next to prior
decisions and cite who said them.

## Raven noticed

A background pass that reads what people told Raven and the decisions they
made next to the indexed code, and raises at most four findings:

- a **gap**: someone said the product must do something the code doesn't do;
- a **bug**: the code does something a recorded decision rules out;
- a **decision**: a call an agent will hit soon that nobody has made;
- a **follow-up**: something worth asking more about before building.

Each finding must quote a person (the quote is checked against what they
actually said) and, for gaps and bugs, name files that exist in the repo.
Findings that fail either check are dropped. Nothing changes until a
person picks the next step:

- **Ask <person>** starts a Raven task with the finding's question; Raven
  routes it the way it routes an agent's question.
- **Hand to Claude** copies a ready prompt; Claude can also call
  `bridge_raven_noticed` to read the findings with their quotes.
- **Dismiss**.

It runs on the real model only (`ANTHROPIC_API_KEY`, or
`BRIDGE_MODEL_API=claude-cli`); with none it refuses rather than showing a
canned list. Ways to start a pass:

- automatically after a voice intro is saved (`BRIDGE_MUSE=off` turns this off);
- "Look again" on the Ask Raven home, or `POST /api/muse/run`;
- `python -m bridge --db <db> muse [--repo owner/name]` from a shell;
- `bridge_raven_noticed` with `run: true` from an agent.

`GET /api/muse` lists open findings; `POST /api/muse/<id>/ask|dismiss|done`
acts on one.

## New MCP tools

| Tool | What it's for |
|---|---|
| `bridge_ask_raven` | A plain question over decisions, sources and what people said, with citations and who to talk to |
| `bridge_person_context` | One person's page: what they decide, wrote, said and decided |
| `bridge_find_people` | Who to talk to about a topic, and what people already said about it |
| `bridge_remember` | Keep something a person told the agent as part of their context |
| `bridge_raven_noticed` | Read (or re-run) the background findings, each with a ready agent prompt |
