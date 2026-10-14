"""Credential destination and retry contracts; only synthetic offline responses."""
import io
import json
import os
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from unittest.mock import Mock, call, patch

from bridge import llm
from bridge.config import Config


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/responses"


class RecordingHTTPSHandler(urllib.request.HTTPSHandler):
    """Exercise the real opener/redirect chain without opening any sockets."""
    def __init__(self, status=200, location=None):
        super().__init__()
        self.requests = []
        self.status = status
        self.location = location
        self.responses = []

    def https_open(self, request):
        self.requests.append(request)
        headers = Message()
        if self.location is not None:
            headers["Location"] = self.location
        response = urllib.response.addinfourl(
            io.BytesIO(b'{"ok": true}'), headers, request.full_url, self.status)
        response.msg = "fixture response"
        self.responses.append(response)
        return response


class ModelTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = llm.Client(Config())
        self.build_opener = urllib.request.build_opener

    def test_official_requests_ignore_inherited_proxies_and_global_opener(self):
        for url, headers in (
                (ANTHROPIC_URL, {"x-api-key": "synthetic-anthropic-key"}),
                (OPENAI_URL, {"Authorization": "Bearer synthetic-openai-key"})):
            with self.subTest(url=url):
                handler = RecordingHTTPSHandler()
                private_openers = []

                def build(*handlers):
                    self.assertEqual([h.proxies for h in handlers
                                      if isinstance(h, urllib.request.ProxyHandler)], [{}])
                    opener = self.build_opener(*handlers, handler)
                    private_openers.append(opener)
                    return opener

                global_opener = Mock()
                global_opener.open.side_effect = AssertionError("Global opener must not be used")
                with patch.dict(os.environ, {
                        "HTTP_PROXY": "http://unexpected-proxy.invalid:8000",
                        "HTTPS_PROXY": "http://unexpected-proxy.invalid:8000",
                        "ALL_PROXY": "http://unexpected-proxy.invalid:8000",
                        "https_proxy": "http://unexpected-proxy.invalid:8000", "NO_PROXY": ""}), \
                        patch("urllib.request._opener", global_opener), \
                        patch("urllib.request.getproxies", side_effect=AssertionError("Proxy discovery forbidden")), \
                        patch("urllib.request.build_opener", side_effect=build):
                    self.assertEqual(self.client._post_json(url, headers, {"fixture": True}, timeout=9),
                                     {"ok": True})
                global_opener.open.assert_not_called()
                self.assertEqual(len(private_openers), 1)
                self.assertEqual(len(handler.requests), 1)
                request = handler.requests[0]
                self.assertEqual(request.full_url, url)
                self.assertFalse(request.has_proxy())
                self.assertIsNone(request._tunnel_host)
                self.assertEqual(request.timeout, 9)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(json.loads(request.data), {"fixture": True})
                for name, value in headers.items():
                    self.assertEqual(request.get_header(name.capitalize()), value)

    def test_unapproved_destinations_fail_before_request_creation_or_transport(self):
        for url in (
                "http://api.anthropic.com/v1/messages",
                "https://api.anthropic.com:443/v1/messages",
                "https://api.anthropic.com/v1/messages?key=private-canary",
                "https://api.anthropic.com/v1/messages#private-canary",
                "https://api.anthropic.com/v1/messages/",
                "https://api.anthropic.com./v1/messages",
                "https://api.anthropic.com.evil.invalid/v1/messages",
                "https://api.anthropic.com@evil.invalid/v1/messages",
                "https://private-canary=@api.anthropic.com/v1/messages",
                "https://api.openai.com/v1/chat/completions",
                "http://127.0.0.1:8765/v1/messages",
                "file:///private-canary", " " + ANTHROPIC_URL, "\n" + OPENAI_URL):
            with self.subTest(url=url), patch("bridge.llm._open_model_request") as transport, \
                    patch("urllib.request.Request") as request:
                with self.assertRaisesRegex(llm.LLMError, "destination refused") as caught:
                    self.client._post_json(url, {"x-api-key": "synthetic-key"}, {})
                self.assertNotIn("private-canary", str(caught.exception))
                request.assert_not_called()
                transport.assert_not_called()

    def test_transport_seam_itself_rejects_an_unapproved_request(self):
        request = urllib.request.Request("https://unexpected.invalid/v1/messages", data=b'{}')
        with patch("urllib.request.build_opener") as build:
            with self.assertRaisesRegex(llm.LLMError, "destination refused"):
                llm._open_model_request(request)
        build.assert_not_called()

    def test_every_redirect_is_refused_without_replaying_credentials_or_retrying(self):
        for status in (301, 302, 303, 307, 308):
            for destination in ("https://unexpected.invalid/credential-canary", OPENAI_URL, ANTHROPIC_URL):
                with self.subTest(status=status, destination=destination):
                    handler = RecordingHTTPSHandler(status, destination)
                    with patch("urllib.request.build_opener", side_effect=lambda *handlers:
                               self.build_opener(*handlers, handler)), patch("time.sleep") as sleep:
                        with self.assertRaisesRegex(llm.LLMError, "redirect refused") as caught:
                            self.client._post_json(ANTHROPIC_URL, {"x-api-key": "synthetic-key"}, {})
                    self.assertEqual(len(handler.requests), 1)
                    self.assertEqual(handler.requests[0].full_url, ANTHROPIC_URL)
                    self.assertTrue(handler.responses[0].closed)
                    self.assertNotIn("credential-canary", str(caught.exception))
                    sleep.assert_not_called()

    def test_retryable_http_errors_keep_backoff_and_four_attempt_limit(self):
        for status in (408, 429, 500, 502, 503, 529):
            with self.subTest(status=status):
                errors = [urllib.error.HTTPError(ANTHROPIC_URL, status, "fixture", {},
                          io.BytesIO(b'{"error":{"message":"fixture failure"}}')) for _ in range(4)]
                with patch("bridge.llm._open_model_request", side_effect=errors) as transport, \
                        patch("time.sleep") as sleep:
                    with self.assertRaisesRegex(llm.LLMError, "Tried 4 times"):
                        self.client._post_json(ANTHROPIC_URL, {}, {})
                self.assertEqual(transport.call_count, 4)
                self.assertEqual(sleep.call_args_list, [call(1.0), call(2.0), call(4.0)])

    def test_retryable_network_errors_recover_without_changing_payload(self):
        for error in (urllib.error.URLError("fixture"), TimeoutError("fixture"), OSError("fixture")):
            with self.subTest(error=type(error).__name__), \
                    patch("bridge.llm._open_model_request", side_effect=[error, io.BytesIO(b'{"ok":true}')]) as transport, \
                    patch("time.sleep") as sleep:
                self.assertEqual(self.client._post_json(OPENAI_URL, {}, {"fixture": True}), {"ok": True})
                self.assertEqual(transport.call_count, 2)
                self.assertEqual(transport.call_args_list[0].args[0].data,
                                 transport.call_args_list[1].args[0].data)
                sleep.assert_called_once_with(1.0)

    def test_nonretryable_http_error_still_raises_llm_error_immediately(self):
        error = urllib.error.HTTPError(OPENAI_URL, 401, "fixture", {},
                                      io.BytesIO(b'{"error":{"message":"fixture auth failure"}}'))
        with patch("bridge.llm._open_model_request", side_effect=error) as transport, patch("time.sleep") as sleep:
            with self.assertRaisesRegex(llm.LLMError, "model API error 401: fixture auth failure"):
                self.client._post_json(OPENAI_URL, {}, {})
        transport.assert_called_once()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
