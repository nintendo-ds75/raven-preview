# Typed-context validation

The typed-context contract and nine initial controls were written before implementation. The initial run showed **one semantic failure** (missing current namespace did not trigger clarification) and **eight unsupported-input API errors**. Those API errors are not counted as eight behavioral failures.

The matching candidate passed **17 typed-context and 31 existing contact checks**, plus **16 existing scope/conversation compatibility checks**. The ten prior external review probes also passed. A subsequent external review found no incompatible contact transfer in its controls, but identified two incorrect explanations:

- Contradictory historical `org`/`organization` facts correctly excluded a contact while reporting only namespace compatibility.
- Missing-provider clarification correctly withheld the contact but still claimed shared-work-item relevance before compatibility was established.

The latest correction reports the actual material-fact conflict and withholds relationship credit until required scope is known. A third committed regression covers the same premature-credit problem when material facts are missing. All **20 current typed-context tests** pass. The **seven external typed-context probes** pass when rerun; they remain separate from committed-test counts and portable commands.

Run from the repository root:

```sh
PYTHONPATH=tests:. python -m unittest test_contact_context -v
```

## Performance limit

The reader still fetches all contact observations for a repository before applying freshness filtering in Python. An independent one-process SQLite sample with 0, 64, 512, and 2,048 expired observations returned no candidates and used four SELECT statements per read. Median times with allocation tracing were approximately 0.25, 2.28, 14.10, and 49.56 ms; nonempty peak traced allocations were about 0.12, 0.91, and 3.60 MB. The empty allocation result included import overhead and is not comparable.

This demonstrates increasing work for already-ineligible history, not a functional failure or a production capacity result. No performance redesign or silent newest-N truncation was introduced: truncating history could hide matching or contrary evidence. Eligibility filtering before retrieval or an explicit incomplete-result budget remains future work.

Canonical fields are compared as supplied. Organization/customer/domain are not inferred from prose, URLs, or namespace labels. Cross-provider equivalence, source-currentness, expertise, and authority remain outside the contact feature. See the [current verification report](../README.md) for exact committed-test commands and qualification limits.
