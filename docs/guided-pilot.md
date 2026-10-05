# Run one useful task together

Start with a repository your team is comfortable using for a supervised trial. Choose a task whose implementation needs a decision from someone other than its author. Name that person in the ownership map before starting.

A first session has four parts:

1. Set up the instance and connect the coding agent. Your administrator keeps control of credentials and access.
2. Give the agent a real task. Open its overview together and inspect the questions it records.
3. Let the actual owner answer or correct the proposed answer. Watch the agent read it and complete the change. Review and test the patch as you normally would.
4. Try a related question. Check whether Raven remembers the relevant answer, explains its scope, and asks for a fresh signature.

Leave automatic standing-rule authorization off for this workflow.

## Learning from your use

We want to know where Raven makes you do extra work: a missing question, the wrong recipient, a misleading proposal, an unnecessary interruption, or an answer the agent did not follow.

Use the task history to review one incident. It keeps the questions, routing, delivery status, answers and corrections. A successful delivery means the messaging service accepted it, not that someone read it.

You can screen share, describe the incident, or export a trace through the authenticated `GET /api/tasks/{task_id}/trace` endpoint. The agent can also retrieve the task trace. A workspace export contains much more data; use it only when necessary.

Before sending anything outside your team, review it for names, repository paths, source text, customer facts, and private decisions. This preview does not automatically anonymize traces. Share only the evidence your team approves. Keep the original privately so you can compare the sequence with any redacted copy.

If your team wants hands-on support, an administrator can invite a named support person as a viewer to inspect the instance. Viewers can read workspace data, not just one selected task. Use a supervised screen share when that scope is too broad. There is no hidden vendor access or public task link.

## What a useful result looks like

A useful first task reaches the right owner, captures a clear answer, gives that answer back to the agent, and produces a tested patch consistent with it. The next related task should require less explanation. Keep the rough edges visible; a polished overview is not evidence that the implementation is correct.
