import math
import pytest
from unittest.mock import patch
from urllib3.util import Retry
from urllib3.response import HTTPResponse


def response(value=None):
    return HTTPResponse(status=503, headers={} if value is None else {'Retry-After': value})

@pytest.mark.parametrize('value,expected', [('10',10),('200',120)])
def test_default_preserves_existing_wait(value,expected):
    with patch('urllib3.util.retry.time.sleep') as sleep, patch('urllib3.util.retry.random.random') as random:
        Retry(retry_after_max=120).sleep(response(value))
        sleep.assert_called_once_with(expected)
        random.assert_not_called()

@pytest.mark.parametrize('draw,expected', [(0,10),(.25,11),(.75,13),(1,14)])
def test_additive(draw,expected):
    with patch('urllib3.util.retry.time.sleep') as sleep, patch('urllib3.util.retry.random.random',return_value=draw), patch('urllib3.util.retry.random.uniform',return_value=draw*4):
        Retry(retry_after_jitter=4,retry_after_max=120).sleep(response('10'))
        sleep.assert_called_once_with(expected)

@pytest.mark.parametrize('header', ['119','120','999999'])
def test_final_ceiling(header):
    with patch('urllib3.util.retry.time.sleep') as sleep, patch('urllib3.util.retry.random.random',return_value=1), patch('urllib3.util.retry.random.uniform',return_value=4):
        Retry(retry_after_jitter=4,retry_after_max=120).sleep(response(header))
        sleep.assert_called_once_with(120)

@pytest.mark.parametrize('header', [None,'0','Wed, 01 Jan 2020 00:00:00 GMT'])
def test_no_new_wait_for_missing_zero_expired(header):
    retry=Retry(retry_after_jitter=4,backoff_factor=1).increment().increment()
    with patch('urllib3.util.retry.time.sleep') as sleep, patch('urllib3.util.retry.random.random') as random:
        retry.sleep(response(header))
        sleep.assert_called_once_with(2)
        random.assert_not_called()

@pytest.mark.parametrize('bad', [-1, float('nan'),float('inf'),-float('inf')])
def test_invalid_value(bad):
    with pytest.raises((ValueError,TypeError)):
        Retry(retry_after_jitter=bad)

@pytest.mark.parametrize('value',[0,.25,4])
def test_setting_survives_copies(value):
    retry=Retry(retry_after_jitter=value)
    assert retry.new().retry_after_jitter==value
    assert retry.increment().retry_after_jitter==value


def test_backoff_jitter_is_unchanged():
    retry=Retry(retry_after_jitter=10,backoff_jitter=4,backoff_factor=1).increment().increment()
    with patch('urllib3.util.retry.random.random',return_value=.5):
        assert retry.get_backoff_time()==4


def test_disabled_retry_after_falls_back():
    retry=Retry(retry_after_jitter=10,respect_retry_after_header=False,backoff_factor=1).increment().increment()
    with patch('urllib3.util.retry.time.sleep') as sleep:
        retry.sleep(response('100'))
        sleep.assert_called_once_with(2)


def test_future_http_date():
    with patch('urllib3.util.retry.time.time',return_value=1577836800), patch('urllib3.util.retry.random.random',return_value=.5), patch('urllib3.util.retry.random.uniform',return_value=2), patch('urllib3.util.retry.time.sleep') as sleep:
        Retry(retry_after_jitter=4).sleep(response('Wed, 01 Jan 2020 00:00:10 GMT'))
        sleep.assert_called_once_with(12)
