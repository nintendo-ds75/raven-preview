import unittest

from billing.usage import invoice_cents


class UsageTests(unittest.TestCase):
    def test_ordinary_usage(self):
        self.assertEqual(invoice_cents([{"account": "a", "units": 2},
                                        {"account": "a", "units": 3}], 7), {"a": 35})

    def test_zero_usage(self):
        self.assertEqual(invoice_cents([{"account": "a", "units": 0}], 7), {"a": 0})

    def test_negative_rate(self):
        with self.assertRaises(ValueError):
            invoice_cents([], -1)

    def test_negative_units(self):
        with self.assertRaises(ValueError):
            invoice_cents([{"account": "a", "units": -1}], 7)
