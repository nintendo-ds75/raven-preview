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

### Every line points at its source

A line on a person's page is a pointer to the first-class thing it came
from, with a one-line summary: the **voice intro** it was said in, the
**decision call**, the **meeting transcript**, or the **decision** an agent
asked for. Clicking it opens the whole source (`GET /api/sources/<kind>/<id>`)
with the quoted line marked, so anyone can check what was actually said.
Ask Raven's quotes open the same way.

### Meetings

Paste or sync the transcript of a call that just ended (a customer check-in,
a planning sync): `POST /api/meetings/learn` with `{repo, title, transcript}`,
or `bridge_learn_meeting` from Claude. Raven files it as a meeting record and
keeps, on each teammate's page, the asks, numbers and commitments in it, each
quoted from the transcript and pointing back to it. A line whose quote is not
in the transcript is dropped. Nothing becomes a decision.

APIs: `GET /api/people/directory?q=`, `GET /api/people/<id>/profile`,
`POST /api/people/<id>/context` with `{"action": "add"|"edit"|"remove", ...}`.

What people told Raven also answers questions: Ask Raven
(`POST /api/ask`) and `bridge_ask_raven` read these lines next to prior
decisions and cite who said them.

## Raven noticed

A background partner that reads what people told Raven and the decisions
they made next to the indexed code, and raises at most five findings, each
a short title and two sentences:

- a **gap**: someone said the product must do something the code doesn't do;
- a **bug**: the code does something a recorded decision rules out;
- a **decision**: a call an agent will hit soon that nobody has made;
- an **idea**: something worth building that what people said points to;
- **two people should talk**: two people said things that bear on each other;
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

- automatically after a voice intro is saved, and after new answers, notes
  or meetings, at most once every `BRIDGE_MUSE_EVERY` seconds (default 300;
  `BRIDGE_MUSE=off` turns this off);
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
| `bridge_learn_meeting` | File a meeting transcript and keep what each teammate needs from it |
| `bridge_pr_summary` | The decisions behind a change, as a PR description |

## The PR summary

Every task page has a **PR summary** tab (`GET /api/tasks/<id>/pr-summary`,
or `bridge_pr_summary`). One line per decision the change needed:

- **Live reply**: Raven asked a person and they answered for this task, on a
  call, on their task page, in Slack or in Raven. A new judgment.
- **Raven cache hit**: an earlier decision answered it, so nobody was pinged.
  The line names who made that call and in which task.
- **From records**, **Agent default**, **Still open** for the rest.

Each line carries the answer, the reason, and who signed it off. "Copy as PR
description" (or the tool's `markdown`) is the body to paste into GitHub.
`bridge_add_node` and `bridge_get_tree` also return a `raven` line per node
("Answered from memory: Marcus Lee decided this in …", "Asking Priya Shah.").

## Acting user

With sign-in off, the profile at the bottom left says who is using Raven.
"Choose who you are" (or `RAVEN_ACT_AS=<name or email>`, or the `act_as`
setting) makes the local operator show as that person, by name and email,
and tasks an agent starts over MCP are requested by them. It keeps the
operator's standing; it is not a sign-in.

## Asking back before answering

On a task page the person asked sees one or two lines on what the agent is
doing and what it needs, then "Have a question first? Ask Raven". On a call
they can just ask ("how many orders is that a morning?"): Raven answers in a
sentence or two from the task and what people told it, then asks its
question again. The question is not saved as their answer.
