# Intro chats

Raven holds a fifteen-minute, get-to-know-you chat with each person, from
**Intro chats** in the sidebar. The goal is a solid base of context that
today exists only in people's heads.

## How a chat works

1. **Prepare.** Raven reads what the workspace already records about the
   person: their teams and teammates, the authority map, CODEOWNERS, git
   ownership and recent commits. It shows that list, then writes about eight
   questions that skip anything the record already answers.
2. **Talk.** The chat follows an interview arc from broad to specific:
   background, their role, how the team works, their current work, and what
   isn't written down, then a closing question. After each answer Raven
   either follows up on something concrete the person said (at most two
   follow-ups in a row) or moves to the next planned question, and it wraps
   up when the person is done or time runs out. The clock pauses while Raven
   is thinking. People can type or use browser dictation; Raven stores text,
   never audio.
3. **Review.** Raven drafts notes from the answers. Each note quotes the
   answer it came from, and a draft note whose quote isn't in the transcript
   is dropped. The page says how many questions were asked and lists what
   Raven still doesn't know. The person unchecks or rewrites notes before
   anything is saved.

Saved notes are the person's own words, not decisions and not verified
facts. They appear on the person's intro page and are available to Raven as
background through `GET /api/people/:id/context`.

## Models

With a model backend (`ANTHROPIC_API_KEY`, or `BRIDGE_MODEL_API=claude-cli`)
Raven writes personalized questions, follows up and drafts notes. Without
one, the questions come from templates filled in from the record, there are
no follow-ups, and each note is the first sentence of an answer. The
`claude-cli` backend works but takes 20 to 60 seconds a turn; an API key
takes a few seconds.

## API

- `GET /api/intro`: everyone and where their chat stands
- `GET /api/intro/people/:id`: the latest chat, saved notes, and the record
- `POST /api/intro/people/:id/prepare`
- `POST /api/intro/:id/start|answer|skip|finish|save|cancel` with
  `expected_version`; `answer` takes `answer` and `capture_method`; `save`
  takes `notes` (`id`, edited `text`) and optional `added` notes.

Only the person (or the local operator when auth is off) can take their
own chat.

## Demo workspace

`scripts/seed_parcelwise_demo.py` seeds a six-person demo company with
eight months of git history, an authority map, and four past decisions.
`--company` renames it; `--existing --repo PATH --repo-name NAME` ingests
an existing checkout instead of building one.
