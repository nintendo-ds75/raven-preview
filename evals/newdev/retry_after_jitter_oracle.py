"""Independent behavioral oracle for the connector evaluation's urllib3 patch.

Run with a Python environment importing the patched urllib3 checkout. The host
never saw this script. Expected behavior comes from the six owner answers, not
from the host's tests. No real sleeps or network requests are made.
"""
import json
from email.utils import formatdate
from unittest.mock import patch

from urllib3 import HTTPResponse
from urllib3.util import Retry


def response(header=None):
    return HTTPResponse(status=503, headers={} if header is None else {"Retry-After": header})


def main():
    results = []

    def check(name, fn):
        try:
            observations = fn()
            results.append({"name": name, "passed": True, "observations": observations})
        except Exception as exc:
            results.append({"name": name, "passed": False, "error": repr(exc)})

    def bounds():
        count = 0
        for raw in (1, 8, 10, 100):
            for cap in (3, 10, 120):
                for amount in (0.0, 0.5, 5.0):
                    for draw in (0.0, 0.25, 0.999999):
                        retry = Retry(retry_after_jitter=amount, retry_after_max=cap)
                        with patch("urllib3.util.retry.random.random", return_value=draw) as rng, patch("urllib3.util.retry.time.sleep") as sleep:
                            assert retry.sleep_for_retry(response(str(raw))) is True
                            waited = sleep.call_args.args[0]
                            base = min(raw, cap)
                            assert base <= waited <= cap, (raw, cap, amount, draw, waited)
                            assert waited == min(base + amount * draw, cap)
                            assert rng.call_count == bool(amount)
                        count += 1
        return {"boundary_combinations": count}

    def dates():
        epoch = 1_700_000_000
        for seconds in (-10, 0, 1, 60):
            retry = Retry(retry_after_jitter=4, retry_after_max=100)
            with patch("urllib3.util.retry.time.time", return_value=epoch), patch("urllib3.util.retry.random.random", return_value=0.5) as rng, patch("urllib3.util.retry.time.sleep") as sleep:
                assert retry.sleep_for_retry(response(formatdate(epoch + seconds, usegmt=True))) is (seconds > 0)
                assert rng.call_count == (seconds > 0)
                if seconds > 0:
                    sleep.assert_called_once_with(seconds + 2)
                else:
                    sleep.assert_not_called()
        return {"date_cases": 4}

    def parsing():
        retry = Retry(retry_after_jitter=5, retry_after_max=10)
        with patch("urllib3.util.retry.random.random") as rng, patch("urllib3.util.retry.time.sleep") as sleep:
            for _ in range(3):
                assert retry.get_retry_after(response("60")) == 10
                assert retry.parse_retry_after("60") == 10
            rng.assert_not_called()
            sleep.assert_not_called()

    def fresh():
        retry = Retry(retry_after_jitter=4)
        with patch("urllib3.util.retry.random.random", side_effect=[0.25, 0.75]), patch("urllib3.util.retry.time.sleep") as sleep:
            retry.sleep_for_retry(response("10"))
            retry.sleep_for_retry(response("10"))
            assert [c.args[0] for c in sleep.call_args_list] == [11, 13]

    def absent():
        with patch("urllib3.util.retry.random.random") as rng, patch("urllib3.util.retry.time.sleep") as sleep:
            retry = Retry(retry_after_jitter=4)
            for header in (None, "0"):
                assert retry.sleep_for_retry(response(header)) is False
            rng.assert_not_called()
            sleep.assert_not_called()

    def carry():
        retry = Retry(total=5, retry_after_jitter=2.5, retry_after_max=7)
        for clone in (retry.new(), retry.increment(method="GET"), retry.increment(method="POST").increment(method="GET")):
            assert clone.retry_after_jitter == 2.5
            assert clone.retry_after_max == 7
        assert retry.new(retry_after_jitter=0).retry_after_jitter == 0
        assert retry.new(retry_after_jitter=6).retry_after_jitter == 6
        assert retry.retry_after_jitter == 2.5

    def validation():
        for value in (-0.01, float("nan"), float("inf"), -float("inf")):
            for make in (lambda: Retry(retry_after_jitter=value), lambda: Retry().new(retry_after_jitter=value)):
                try:
                    make()
                except ValueError:
                    continue
                raise AssertionError(f"accepted {value}")
        return {"rejected_inputs": 8}

    def fallback():
        for jitter in (0, 5):
            retry = Retry(total=5, backoff_factor=1, backoff_jitter=2, retry_after_jitter=jitter)
            retry = retry.increment(method="GET").increment(method="GET")
            with patch("urllib3.util.retry.random.random", return_value=0.5), patch("urllib3.util.retry.time.sleep") as sleep:
                retry.sleep(response())
                sleep.assert_called_once_with(3)
        retry = Retry(total=5, respect_retry_after_header=False, retry_after_jitter=5, backoff_factor=1)
        retry = retry.increment(method="GET").increment(method="GET")
        with patch("urllib3.util.retry.random.random") as rng, patch("urllib3.util.retry.time.sleep") as sleep:
            retry.sleep(response("60"))
            sleep.assert_called_once_with(2)
            rng.assert_not_called()

    for name, fn in (("numeric boundaries and disabled RNG", bounds), ("HTTP-date and expired values", dates),
                     ("public parsing remains deterministic", parsing), ("fresh sample each sleep", fresh),
                     ("zero and missing headers", absent), ("new/increment carry and override", carry),
                     ("invalid finite/nonnegative values", validation), ("backoff independence and opt-out", fallback)):
        check(name, fn)
    print(json.dumps({"passed": sum(x["passed"] for x in results), "total": len(results), "results": results}, indent=2))
    return 0 if all(x["passed"] for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
