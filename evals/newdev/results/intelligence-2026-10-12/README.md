# Live intelligence probe, October 12, 2026

The input is urllib3/urllib3 at `3d934808397b9cee14c6f399a4e6c9ddeb17a00d`, with 250 pre-revision commits. Identities are pseudonymized before inference. Runtime and documentation policies and the two reachable contacts are synthetic. Nobody in the public repository was contacted.

Run `python -m evals.newdev.intelligence_probe --repo CHECKOUT --rev REVISION --output REPORT` with inference credentials configured privately and the semantic dependencies installed.

| Run | Seconds | Result |
| --- | ---: | --- |
| first.json | 95.09 | Seven of eight checks; the contact model lacked record text for candidates already nominated by history. |
| second.json | 82.75 | Contact fixed; discovery output failed validation and was explicitly unavailable. The rejected response was not retained in this earlier revision, so the exact offending field cannot be established. |
| final.json | 99.53 | Eight of eight component checks after enriching candidate evidence and clarifying the exact evidence-ID/path contract. |

The final review suggested five findings. Two were code investigations. Of the three labelled judgment, the internal `sleep_for_retry` return-value question should first be investigated by the host; it is not automatically a useful question for a person. Raven does not send these suggestions or create decisions automatically. The host adopts a decision or records why none is needed.

These runs use real inference and local learned embeddings. They are not an autonomous coding-host run, a real Slack installation, a real OpenAI cloud wake-up, or an estimate of production routing accuracy. Keep the unsuccessful runs when comparing improvements.
