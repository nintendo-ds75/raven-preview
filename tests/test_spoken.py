"""Raven says initialisms letter by letter: the speech engines read "USPS" as
a word and "ADR" as "adder" unless the letters are dotted."""
import unittest

from raven.voice import spoken


class SpokenTests(unittest.TestCase):
    def test_initialisms_are_dotted_letters(self):
        self.assertEqual(spoken("How fast may we push USPS label purchases?"),
                         "How fast may we push U.S.P.S. label purchases?")
        self.assertEqual(spoken("Two ADRs and a PR settle it."), "Two A.D.R.s and a P.R. settle it.")

    def test_words_in_capitals_stay_words(self):
        self.assertEqual(spoken("OK, at 9 AM. NASA is a word."), "OK, at 9 AM. NASA is a word.")
        self.assertEqual(spoken("Bloom & Co stays off"), "Bloom & Co stays off")
