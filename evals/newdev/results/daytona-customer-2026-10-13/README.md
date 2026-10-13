# Daytona customer test evidence

See [the review](../../../../reviews/main-82a1a9c-daytona-customer.md) for setup,
results, failed stages and limits. `trace.json` preserves the normalized host/tool
receipts and signed answers. Embedding vectors and duplicate message blobs are
omitted. `source-sha256.json` fingerprints the uploaded Raven source snapshot.

`customer.patch` is an experimental change to a frozen ItsDangerous checkout,
not a change Raven pushed upstream. `independent_contract.py` was written by the
test operator, separately from the coding host. The result and restore JSON files
bind to the final patch hash. Screenshots show the actual browser result.

`earlier-hybrid-idle-result.json` belongs to the preceding local-server plus
Daytona-executor probe. It must not be described as a measurement from the full
customer Docker deployment. Synthetic people and policy are not real approvals
from the upstream project. No credentials, private brief or database backup is
included here.
