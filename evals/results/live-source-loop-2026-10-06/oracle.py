import unittest,os
from unittest.mock import patch
from urllib3.util.retry import Retry
from urllib3.exceptions import InvalidHeader

class PolicyContract(unittest.TestCase):
    def test_fractional_and_ordinary_delays(self):
        for text,value in [('0.5',.5),('0',0),('3',3),(' 1.25 ',1.25),('0003.50',3.5)]:
            with self.subTest(text=text):self.assertEqual(Retry().parse_retry_after(text),value)
    def test_plus_policy_revision(self):
        for text in ['+3','+0.5']:
            with self.subTest(text=text):
                if os.environ.get('POLICY_REVISION')=='2':
                    with self.assertRaises(InvalidHeader):Retry().parse_retry_after(text)
                else:self.assertEqual(Retry().parse_retry_after(text),float(text))
    def test_nonfinite_negative_exponent_rejected(self):
        for text in ['-1','-0.5','1e3','NaN','inf','Infinity','garbage','1.2.3']:
            with self.subTest(text=text):
                with self.assertRaises(InvalidHeader):Retry().parse_retry_after(text)
    def test_http_date_unchanged(self):
        with patch('urllib3.util.retry.time.time',return_value=0):
            self.assertEqual(Retry(retry_after_max=100).parse_retry_after('Thu, 01 Jan 1970 00:00:07 GMT'),7)
    def test_cap_kept(self):
        self.assertEqual(Retry(retry_after_max=2).parse_retry_after('8.5'),2)
    def test_no_new_retry_methods(self):
        self.assertFalse(Retry()._is_method_retryable('POST'))
    def test_zero_still_falls_back_to_backoff(self):
        from urllib3.response import HTTPResponse
        r=Retry(backoff_factor=1)
        with patch.object(r,'_sleep_backoff') as fallback:
            r.sleep(HTTPResponse(headers={'Retry-After':'0.0'}))
            fallback.assert_called_once()

if __name__=='__main__': unittest.main(verbosity=2)
