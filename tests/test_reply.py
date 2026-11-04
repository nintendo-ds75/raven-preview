"""One box on the For you card: the call and its reason are filed apart from
one typed reply, without a model."""
import unittest

from raven.reply import split_reply


class SplitReplyTests(unittest.TestCase):
    def test_a_second_sentence_that_explains_is_the_reason(self):
        self.assertEqual(split_reply("Four cents for each answer beyond the allowance, on Starter and Growth. "
                                     "That is about three times what an answer costs us."),
                         ("Four cents for each answer beyond the allowance, on Starter and Growth.",
                          "That is about three times what an answer costs us."))

    def test_because_inside_the_sentence_splits_it(self):
        self.assertEqual(split_reply("Count only completed answers, because retries hit the model again and cost us"),
                         ("Count only completed answers.", "Because retries hit the model again and cost us"))

    def test_conditions_stay_in_the_call(self):
        answer, why = split_reply("Two hundred answers a month on Starter, a thousand on Growth. "
                                  "Bloom and Co stays off the public rate until Sam clears their order form. "
                                  "Most shops never reach it. The heavy ones are the ones costing us.")
        self.assertEqual(answer, "Two hundred answers a month on Starter, a thousand on Growth. "
                                 "Bloom and Co stays off the public rate until Sam clears their order form.")
        self.assertEqual(why, "Most shops never reach it. The heavy ones are the ones costing us.")

    def test_a_reply_with_no_reason_is_all_call(self):
        self.assertEqual(split_reply("December. We bill in arrears, so the first invoice goes out in January."),
                         ("December.", "We bill in arrears, so the first invoice goes out in January."))
        self.assertEqual(split_reply("No exceptions."), ("No exceptions.", ""))
        self.assertEqual(split_reply("  "), ("", ""))

    def test_a_labelled_why_loses_its_label(self):
        self.assertEqual(split_reply("Read usage from usage_daily. Why: it is the table ADR 0005 names."),
                         ("Read usage from usage_daily.", "it is the table ADR 0005 names."))
