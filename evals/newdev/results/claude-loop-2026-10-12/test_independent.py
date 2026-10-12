from unittest.mock import patch
import math
import pytest
from urllib3.util.retry import Retry
from urllib3.response import HTTPResponse

def response(value="300"):
    return HTTPResponse(status=429, headers={"Retry-After":value})

def test_default_unlimited():
    r=Retry()
    with patch("urllib3.util.retry.time.sleep") as sleep:
        r.sleep(response())
        sleep.assert_called_once_with(300)

def test_positive_cap_and_raw_parsing():
    r=Retry(retry_after_max=2.5)
    assert r.parse_retry_after("300")==300
    assert r.get_retry_after(response())==300
    with patch("urllib3.util.retry.time.sleep") as sleep:
        assert r.sleep_for_retry(response()) is True
        sleep.assert_called_once_with(2.5)

def test_shorter_header_not_extended():
    with patch("urllib3.util.retry.time.sleep") as sleep:
        Retry(retry_after_max=100).sleep(response("3"))
        sleep.assert_called_once_with(3)

def test_zero_still_handles_header_without_exponential_fallback():
    r=Retry(total=5,backoff_factor=2,retry_after_max=0).increment().increment()
    assert r.get_backoff_time()>0
    with patch("urllib3.util.retry.time.sleep") as sleep:
        assert r.sleep_for_retry(response()) is True
        r.sleep(response())
        assert all(call.args[0]==0 for call in sleep.call_args_list)

def test_clones_preserve_cap():
    r=Retry(retry_after_max=2.5)
    assert r.new().retry_after_max==2.5
    assert r.increment().retry_after_max==2.5
    assert r.new(retry_after_max=3).retry_after_max==3

@pytest.mark.parametrize("value",[-1,float("nan"),float("inf"),float("-inf")])
def test_invalid_at_construction(value):
    with pytest.raises(ValueError):Retry(retry_after_max=value)

def test_backoff_unchanged_without_header():
    r=Retry(total=5,backoff_factor=2,retry_after_max=.5).increment().increment()
    with patch("urllib3.util.retry.time.sleep") as sleep:
        r.sleep(HTTPResponse(status=500))
        sleep.assert_called_once_with(r.get_backoff_time())

def test_disabled_respect_still_uses_backoff():
    r=Retry(total=5,backoff_factor=2,retry_after_max=.5,respect_retry_after_header=False).increment().increment()
    with patch("urllib3.util.retry.time.sleep") as sleep:
        r.sleep(response())
        sleep.assert_called_once_with(r.get_backoff_time())
