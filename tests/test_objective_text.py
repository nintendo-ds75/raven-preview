"""The readable scope of an objective-scope decision: a few sentences and
short lists from the governance JSON the decision stores, never the JSON
itself, and never an exception for a malformed context."""
import json
import unittest

from bridge import objective_text as ot

GOV = {
    'title': 'Draft the renewal note for Acme',
    'outcome': 'Acme renews before the 30th with the usage tier they asked for.',
    'success_criteria': 'A note the account owner can send without edits.',
    'stop_criteria': 'Any pricing change or an unanswered legal question.',
    'guardrails': 'Private preparation only; no sending, no pricing changes.',
    'repository': 'acme/platform', 'provider': 'claude',
    'why_now': 'The renewal is due in nine days.',
    'judgment_kind': 'interpretation_tradeoff',
    'cases': {'positive': '- Draft the note\n- List the usage tier they asked for',
              'negative': 'Send anything to Acme', 'boundary': 'A tier change needs a new judgment',
              'non_implications': 'Does not mean the price is agreed'},
    'citations': [{'record_id': 'r1', 'source_version_id': 'v1', 'quote': 'We want to renew by the 30th at the usage tier.'}],
    'research_digest': 'a' * 64, 'source_dependencies_digest': 'b' * 64,
}
REF = 'objective-acceptance:prop1234'


class ParseTests(unittest.TestCase):
    def test_marked_context_with_trailers_parses_and_records_the_proposal_id(self):
        gov = ot.parse(json.dumps(GOV) + '\nPaths: a.py\nOptions: Yes | No', 'objective_scope', REF)
        self.assertEqual(gov['outcome'], GOV['outcome'])
        self.assertEqual(gov['proposal_id'], 'prop1234')

    def test_unmarked_context_parses_by_shape_only(self):
        self.assertIsNotNone(ot.parse(json.dumps(GOV)))
        self.assertIsNone(ot.parse(json.dumps({'a': 1})), 'an ordinary JSON context is not a scope')
        self.assertIsNone(ot.parse(json.dumps({'cases': {}})), 'cases alone are not a scope')

    def test_non_json_and_malformed_input_give_none(self):
        for context in ('Usage jumped 11x during the rollout.', '{"outcome": ', '[1, 2]', '', None, 7, '{"outcome": 1, "cases": []}' + 'x'):
            self.assertIsNone(ot.parse(context, 'objective_scope', REF), repr(context))
        self.assertIsNone(ot.parse('plain text', client_ref=REF))


class SummaryTests(unittest.TestCase):
    def test_summary_is_at_most_three_sentences_naming_the_move_question_and_bound(self):
        text = ot.summary(ot.parse(json.dumps(GOV), 'objective_scope', REF))
        self.assertIn('Raven proposes to take point on Draft the renewal note for Acme, so that Acme renews', text)
        self.assertIn('should Raven take point on this?', text)
        self.assertIn('Within: Private preparation only', text)
        self.assertIn('Stop when: Any pricing change', text)
        self.assertNotIn('{', text)
        self.assertLessEqual(len([s for s in text.replace('?', '.').split('. ') if s.strip()]), 3)

    def test_missing_fields_are_left_out_not_invented(self):
        text = ot.summary({'outcome': 'Acme renews.'})
        self.assertEqual(text, 'Raven proposes to take point so that Acme renews. The question: should Raven take point on this?')
        self.assertEqual(ot.summary({}), 'Raven proposes to take point on an objective it has not described. The question: should Raven take point on this?')
        self.assertEqual(ot.summary(None), '')
        self.assertEqual(ot.summary('text'), '')


class CardTests(unittest.TestCase):
    def test_card_lists_cases_options_evidence_and_the_today_link(self):
        card = ot.card(ot.parse(json.dumps(GOV), 'objective_scope', REF))
        self.assertEqual(card['question'], 'Should Raven take point on Draft the renewal note for Acme?')
        self.assertEqual(card['options'], ['Yes, take point', 'Not now', 'Change the scope'])
        self.assertEqual(card['cases']['positive'], ['Draft the note', 'List the usage tier they asked for'])
        self.assertEqual(card['cases']['negative'], ['Send anything to Acme'])
        self.assertEqual(card['cases']['boundary'], ['A tier change needs a new judgment'])
        self.assertEqual(card['cases']['non_implication'], ['Does not mean the price is agreed'])
        self.assertEqual(card['guardrails'], GOV['guardrails'])
        self.assertEqual(card['evidence'], [{'quote': 'We want to renew by the 30th at the usage tier.', 'title': ''}])
        self.assertEqual(card['link_to_today'], '#today/prop1234')

    def test_card_keeps_source_titles_and_prefers_the_objective_id(self):
        gov = {**GOV, 'objective_id': 'obj9', 'citations': [{'quote': 'q', 'title': 'Renewal thread'}, 'junk', {'quote': ''}]}
        card = ot.card(gov)
        self.assertEqual(card['evidence'], [{'quote': 'q', 'title': 'Renewal thread'}])
        self.assertEqual(card['link_to_today'], '#today/obj9')

    def test_card_from_missing_or_wrong_fields_is_empty_not_raised(self):
        for gov in ({}, None, {'cases': 'not a dict', 'citations': {'quote': 'x'}, 'outcome': 3}):
            card = ot.card(gov)
            self.assertEqual(card['outcome'], '')
            self.assertEqual(card['cases'], {'positive': [], 'negative': [], 'boundary': [], 'non_implication': []})
            self.assertEqual(card['evidence'], [])
            self.assertEqual(card['link_to_today'], '')
            self.assertEqual(card['question'], 'Should Raven take point on this?')

    def test_cases_given_as_lists_are_kept(self):
        card = ot.card({'cases': {'positive': ['one', ' two ', 3, '']}})
        self.assertEqual(card['cases']['positive'], ['one', 'two'])


class SlackTests(unittest.TestCase):
    def test_slack_text_has_the_scope_cases_and_a_quote_without_json(self):
        text = ot.slack_text(ot.parse(json.dumps(GOV), 'objective_scope', REF))
        self.assertLessEqual(len(text), 700)
        self.assertNotIn('{', text)
        self.assertNotIn('research_digest', text)
        self.assertIn('Will: Draft the note; List the usage tier they asked for', text)
        self.assertIn("Won't: Send anything to Acme", text)
        self.assertIn('Edge: A tier change needs a new judgment', text)
        self.assertIn('Evidence: “We want to renew by the 30th at the usage tier.”', text)

    def test_oversized_quotes_and_fields_are_trimmed_to_the_limit(self):
        gov = {**GOV, 'outcome': 'outcome ' * 300, 'guardrails': 'guard ' * 300, 'stop_criteria': 'stop ' * 300,
               'cases': {k: ('case word ' * 100) for k in ('positive', 'negative', 'boundary', 'non_implications')},
               'citations': [{'record_id': 'r1', 'source_version_id': 'v1', 'quote': 'quote word ' * 200}]}
        text = ot.slack_text(gov)
        self.assertLessEqual(len(text), 700)
        self.assertTrue(text.startswith('Raven proposes to take point on'))
        self.assertIn('…', text)
        self.assertNotIn('{', text)
        card = ot.card(gov)
        self.assertLessEqual(len(card['evidence'][0]['quote']), 1500)

    def test_slack_text_never_raises(self):
        self.assertEqual(ot.slack_text(None), '')
        self.assertEqual(ot.slack_text([]), '')
        self.assertTrue(ot.slack_text({}).startswith('Raven proposes'))


if __name__ == '__main__':
    unittest.main()
