# Proposal bench corpus

A synthetic, labelled corpus for `tests/test_proposal_bench.py`. Every
person, company, message and task in it is invented; there are no real
names, no real companies and no secrets.

- `corpus.json`: twenty records for the repository `sample/project` in the
  shapes Raven stores them: pasted customer emails, captured Slack
  messages, one meeting transcript chunk, two notes and one `session`
  record written the way `bridge/canvas.py` `session_record()` writes one.
  Each record has a fixture `id` (`r01`..`r20`) that the tests map to the
  record id the store assigns.
- `labels.json`: for each record, whether a person would want it surfaced
  as a move; for the positives, the one expected decision title and the
  verbatim quote the proposal must cite; for every record, the `pitch` an
  undiscriminating model double makes when it is scripted to propose on
  every record (its disposition, and a quote that is sometimes invented
  on purpose).

What the bench measures is the plumbing between a model's output and a
stored proposal, and the shape of what a person then reads: that a
citation must be the record's own words, that a proposal carries the
four cases and the card its options, that a repository has at most one
open proposal, that a dismissed proposal is not pitched again unchanged,
and the precision and recall of that gate over these labels when the
double pitches on everything. The model in the tests is a script, so
none of the numbers say anything about model quality; live precision is
reported from real proposals, never from this corpus.
