"""Bounded Vexa REST client. Never joins on construction; no automatic retries."""
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .store import Invalid


class OutcomeUnknown(Invalid):
    """A mutation may have reached the provider; reconcile before retrying."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Vexa:
    provider = 'vexa'

    def __init__(self, base=None, key=None):
        self.base = (base or os.environ.get('BRIDGE_VEXA_URL', '')).rstrip('/')
        self.key = key if key is not None else os.environ.get('VEXA_API_KEY', '')
        p = urlsplit(self.base)
        if (not p.hostname or p.username or p.password or p.query or p.fragment or p.path
                or p.scheme not in ('https', 'http')
                or (p.scheme == 'http' and p.hostname not in ('localhost', '127.0.0.1', 'vexa'))):
            raise Invalid('Configure an HTTPS Vexa origin or local HTTP localhost/vexa origin')
        if not self.key or any(c in self.key for c in '\r\n'):
            raise Invalid('Configure VEXA_API_KEY in the server environment')

    def _request(self, method, path, body=None):
        req = Request(self.base + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={'X-API-Key': self.key, 'Content-Type': 'application/json'})
        mutation = method != 'GET'
        try:
            with build_opener(NoRedirect).open(req, timeout=15) as response:
                raw = response.read(8_388_609)
            if len(raw) > 8_388_608:
                raise ValueError('response limit')
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError('response shape')
            return result
        except HTTPError as error:
            # 5xx may occur after dispatch. Never blindly repeat mutations.
            cls = OutcomeUnknown if mutation and error.code >= 500 else Invalid
            raise cls(f'Vexa HTTP {error.code}; response body withheld') from None
        except (URLError, OSError, TimeoutError, ValueError):
            cls = OutcomeUnknown if mutation else Invalid
            raise cls('Vexa response unavailable or invalid; reconcile before repeating mutations') from None

    @staticmethod
    def _path(meeting):
        return quote(meeting['platform'], safe='') + '/' + quote(meeting['native_meeting_id'], safe='')

    def join(self, meeting, consent, options=None):
        options = options or {}
        body = {'platform': meeting['platform'], 'native_meeting_id': meeting['native_meeting_id'],
                'bot_name': 'Raven — meeting transcription',
                'transcribe_enabled': consent['transcribe'], 'recording_enabled': consent['record']}
        if options.get('passcode'):
            body['passcode'] = str(options['passcode'])
        if meeting['platform'] == 'zoom':
            url = str(options.get('meeting_url', ''))
            p = urlsplit(url)
            if p.scheme != 'https' or not p.hostname or not (p.hostname == 'zoom.us' or p.hostname.endswith('.zoom.us')) or p.username or p.password:
                raise Invalid('Zoom requires the HTTPS zoom.us meeting URL for this meeting')
            if p.path.rstrip('/').split('/')[-1] != meeting['native_meeting_id']:
                raise Invalid('Zoom URL must identify this meeting')
            body['meeting_url'] = url
        return self._request('POST', '/bots', body)

    def stop(self, meeting):
        return self._request('DELETE', '/bots/' + self._path(meeting))

    def delete_remote(self, meeting):
        if not meeting.get('remote_id'):
            raise Invalid('Remote meeting ID is required for artifact deletion')
        return self._request('DELETE', '/meetings/' + quote(meeting['remote_id'], safe=''))

    def status(self, meeting):
        if meeting.get('remote_id'):
            return {'meeting': self._request('GET', '/meetings/' + quote(meeting['remote_id'], safe=''))}
        return self._request('GET', '/bots/status')

    def transcript(self, meeting):
        if not meeting.get('remote_id'):
            raise Invalid('An exact remote meeting occurrence ID is required for transcript sync')
        return self._request('GET', '/transcripts/by-id/' + quote(meeting['remote_id'], safe=''))


def verify_vexa_signature(raw, headers, secret, *, now=None, window=300):
    """Verification primitive only; no unverified event payload ingestion route."""
    lowered = {k.lower(): v for k, v in headers.items()}
    timestamp = lowered.get('x-webhook-timestamp', '')
    try:
        if not secret or abs((time.time() if now is None else now) - int(timestamp)) > window:
            return False
    except (ValueError, TypeError):
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), timestamp.encode() + b'.' + raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, lowered.get('x-webhook-signature', ''))
