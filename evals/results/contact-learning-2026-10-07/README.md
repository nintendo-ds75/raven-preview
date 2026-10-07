# Contact-learning verification

The current change retains scoped referral chains, separates useful connectors from explicit unsuitability, and compares typed context for contact suggestions. See the [contact-learning contract](../../../docs/contact-learning-contract.md) and [typed-context contract](../../../docs/contact-context-contract.md).

## Reproduce from the repository root

```sh
PYTHONPATH=tests:. python -m unittest test_contact_learning test_contact_context -v
PYTHONPATH=tests:. python -m unittest test_scope_clarification.ScopeClarificationTests test_slack_conversation.ReplySchemaTests test_slack_conversation.ScopeLearningTests.test_learning_respects_customer_and_topic test_slack_conversation.ConversationTests.test_natural_referral_then_final_answer_learns_only_final_person test_slack_conversation.ConversationTests.test_polite_referral_is_not_discarded_when_search_is_available -v
```

Current results: **51 committed contact tests** and **16 existing scope/conversation compatibility tests** passed. Python compilation and `git diff --check` passed. Separately, **17 external review probes** passed when rerun against the current corrections; they are not included in these portable repository commands. Final independent rereview remains separate.

Before the latest explanation-only corrections, the matching implementation passed **1,867 ordinary native tests in 276.067 seconds**, with no failures or skips, in a separate integration checkout. That result qualifies the preceding matching implementation, not a fresh full-suite run of the latest source. No latest-source full native/PostgreSQL result is claimed here.

## Initial reproduction

The original five synthetic checks failed before contact-learning implementation:

- Alpha and Beta were labeled declined after their Alpha → Beta → Gamma referral chain successfully reached Gamma.
- A later one-question opt-out left Alpha's earlier negative feedback reusable.
- An unanswered handoff already suppressed the outgoing contact.
- No ordered structured contact-observation events existed.
- A 181-day-old answer still supplied a learned contact.

The first chain test originally expected alphabetical connector order. It now independently asserts respondent-first and availability of both connectors; a separate controlled-time test checks the documented freshness ordering. The original exact outputs are preserved separately. Public reports summarize them without private executor paths or reviewer artifacts.

See [outcome/adapter corrections](successor/README.md) and [typed-context validation](typed-context/README.md) for later findings and limits. No provider calls, real messages, publication, or authority/eligibility certification are claimed.
