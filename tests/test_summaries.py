"""Everything a person reads from a model (the owner's brief, the proposal
of how they decided before, the answer to a question about a task) starts
from one style: the point first, concrete values, no padding, nothing
about how the text was produced."""
import unittest

from bridge import inquiry, llm


class SummaryStyleTests(unittest.TestCase):
    def test_every_human_facing_prompt_starts_with_the_shared_style(self):
        for name, system in (('brief', llm.BRIEF_SYSTEM), ('proposal', llm.PROPOSAL_SYSTEM), ('inquiry', inquiry.SYSTEM)):
            self.assertTrue(system.startswith(llm.SUMMARY_STYLE), name)

    def test_the_style_leads_with_the_point_and_forbids_padding(self):
        style = llm.SUMMARY_STYLE
        for phrase in ('Lead with the point', 'never pad with what was not provided', 'concrete value',
                       'Do not mention the model, the tools'):
            self.assertIn(phrase, style)
        # Short enough to sit in front of every prompt without crowding out its own rules.
        self.assertLess(len(style.split()), 160)

    def test_the_style_does_not_loosen_the_briefs_own_rules(self):
        # The brief forbids lists and names; the shared style defers to that.
        self.assertIn('when the instructions below allow a list', llm.SUMMARY_STYLE)
        self.assertIn('no bullet points, never address the owner by name', llm.BRIEF_SYSTEM)
