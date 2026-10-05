# Prompt: run the open-source end-to-end test

Give this to a coding agent on the machine that will run the test. It follows
[docs/local-e2e-open-source.md](../../docs/local-e2e-open-source.md) for the setup and
[core-e2e-prompt.md](core-e2e-prompt.md) for the checklist. The person running the test answers as the stand-in
owners in the browser.

```text
Read docs/local-e2e-open-source.md and evals/newdev/core-e2e-prompt.md on main in the Raven repo, and run the end-to-end test they describe on this machine, on prometheus/prometheus.

1. Phase 1: clone Prometheus in full. Run evals/newdev/oss_routing_probe.py without and with --sync (I will put GITHUB_TOKEN in the shell). Add 15 probes of your own across areas, including ones no ownership file covers. Score each pick against Prometheus's MAINTAINERS.md and CODEOWNERS. Keep that output local; it names real people.
2. Phase 2: set Raven up with ./setup exactly as the guide says. Create the six stand-in owners, the authority map and the coordinator, and connect Claude Code with the CLAUDE.md rule.
3. Run the guide's five tasks in order, each as its own Claude Code session in print mode with --max-budget-usd 10. Save each transcript. Never give the coding session a list of decisions or owners.
4. When a stand-in has something to answer, tell me which window and which question, and wait for me to answer in the browser. Do not answer as a stand-in yourself unless I say so.
5. After each task, run the relevant go tests yourself and read the finish report.
6. Write the results to reviews/<date>-local-oss-e2e.md. Include the scoreboard from the guide's "What to record" section. Say what was real, what was simulated, and what you could not test.

Rules:
- Never contact the project's maintainers.
- Never push to, or open anything on, the upstream repository.
- Keys stay in .env and the shell, never in chat or a commit.
- No stand-in gets a real name or GitHub login.
- Do not change Raven's code.
```

To test a different repository, swap `prometheus/prometheus` for another one in the guide's table:
- use the matching probe file in `evals/newdev/oss_probes/`;
- mirror that project's own areas for the stand-ins;
- pick tasks the same way: check the behavior does not exist yet, and that each task needs decisions from more than
  one owner.
