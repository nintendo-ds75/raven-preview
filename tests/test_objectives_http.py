"""Native Today REST flow: real local server, synthetic data, no provider calls."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import fixtures  # noqa: F401


class ObjectiveHTTPTests(unittest.TestCase):
    def test_native_objective_and_meeting_routes_preserve_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('objectives_browser_server.py')),
                str(Path(temp) / 'ui.db')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                line = process.stdout.readline()
                if not line:
                    self.fail('UI fixture did not start: ' + process.stderr.read())
                fixture = json.loads(line)
                base = fixture['url'].split('/#')[0]

                def get(path):
                    with urlopen(base + path, timeout=10) as response:
                        return json.loads(response.read())

                state = get('/api/state')

                def post(path, data, csrf=True):
                    headers = {'Content-Type': 'application/json'}
                    if csrf:
                        headers['X-Bridge-CSRF'] = state['csrf_token']
                    request = Request(base + path, json.dumps(data).encode(), headers=headers)
                    with urlopen(request, timeout=10) as response:
                        return json.loads(response.read())

                overview = get('/api/objectives')
                self.assertEqual(len(overview['objectives']), 1)
                self.assertEqual(len(overview['proposals']), 1)
                self.assertTrue(all(not host['live_verified'] for host in overview['capabilities']))
                for asset in ('objectives.js', 'objectives.css'):
                    with urlopen(base + '/' + asset, timeout=10) as response:
                        self.assertEqual(response.status, 200)
                with self.assertRaises(HTTPError) as failure:
                    post('/api/objectives', {}, csrf=False)
                self.assertEqual(failure.exception.code, 403)
                created = post('/api/objectives', {'request_key': 'http-create', 'title': 'HTTP outcome',
                    'outcome': 'Prepare evidence only', 'repository': fixture['repo'], 'provider': 'claude',
                    'success_criteria': 'A reviewed artifact', 'stop_criteria': 'Ask on unclear policy',
                    'guardrails': 'No publication', 'source_ids': []})
                self.assertEqual(created['lifecycle'], 'active')
                self.assertEqual(created['runs'], [])
                checkpoint = post('/api/objectives/' + created['id'] + '/checkpoint', {
                    'request_key': 'http-checkpoint', 'expected_revision': created['revision'],
                    'summary': 'Waiting on policy', 'next_check_at': '2027-01-01T00:00:00Z', 'waiting_on': 'Policy owner'})
                self.assertEqual(checkpoint['waiting_on'], 'Policy owner')
                self.assertEqual(checkpoint['revision'], created['revision'] + 1)
                with self.assertRaises(HTTPError) as failure:
                    post('/api/objectives/' + created['id'] + '/pause', {
                        'request_key': 'http-stale', 'expected_revision': created['revision']})
                self.assertEqual(failure.exception.code, 400)
                blocked = post('/api/objectives', {'request_key': 'http-start', 'title': 'HTTP prepared outcome',
                    'outcome': 'Prepare evidence only', 'repository': fixture['repo'], 'provider': 'claude',
                    'success_criteria': 'A reviewed artifact', 'stop_criteria': 'Ask on unclear policy',
                    'guardrails': 'No publication', 'source_ids': [], 'start_preparation': True})
                # No host is configured in the fixture: the refusal is recorded
                # as an objective event with a revision bump, not a raw update.
                self.assertTrue(blocked['blocked_reason'])
                self.assertEqual(blocked['revision'], 2)
                self.assertEqual(blocked['runs'], [])
                self.assertEqual([e['summary'] for e in blocked['events'] if e['kind'] == 'dispatch_blocked'], [blocked['blocked_reason']])
                proposal = overview['proposals'][0]
                review = post('/api/proposals/' + proposal['id'] + '/accept', {
                    'request_key': 'http-accept', 'expected_revision': proposal['revision']})
                self.assertEqual(review['status'], 'review')
                self.assertTrue(review['decision_id'])
                self.assertEqual(len(get('/api/objectives')['objectives']), 3)
                # Creating the governance review did not create a fourth active objective.
                meeting = post('/api/meetings', {'repo': fixture['repo'], 'provider': 'import',
                    'namespace': 'http-fixture', 'title': 'HTTP transcript', 'platform': 'import',
                    'native_meeting_id': 'http-meeting'})
                self.assertEqual(meeting['state'], 'planned')
                imported = post('/api/meetings/' + meeting['id'] + '/import', {
                    'repo': fixture['repo'], 'body': 'A synthetic meeting transcript, not approval.',
                    'source_version': 'one', 'workspace_shared': True, 'by': 'Forged actor'})
                self.assertTrue(imported['source']['record_id'])
                self.assertNotIn('record_id', imported['meeting'])
                with sqlite3.connect(Path(temp) / 'ui.db') as database:
                    saved = database.execute("SELECT detail FROM events WHERE kind='meeting_transcript_imported' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertNotEqual(json.loads(saved[0])['by'], 'Forged actor')
                revoked = post('/api/meetings/' + meeting['id'] + '/revoke', {'repo': fixture['repo']})
                self.assertTrue(revoked['revoked'])
                self.assertEqual(revoked['state'], 'planned')
                self.assertNotIn('record_id', revoked)
                self.assertIn('remote_stop_required', revoked)
                with sqlite3.connect(Path(temp) / 'ui.db') as database:
                    saved = database.execute('SELECT revoked,remote_state FROM meeting_occurrences WHERE id=?', (meeting['id'],)).fetchone()
                self.assertEqual(saved, (1, ''))
                capture = post('/api/meetings', {'repo': fixture['repo'], 'provider': 'vexa',
                    'namespace': 'http-fixture', 'title': 'Background fixture', 'platform': 'google_meet',
                    'native_meeting_id': 'abc-defg-hij'})
                endpoint = '/api/meetings/' + capture['id']
                with self.assertRaises(HTTPError):
                    post(endpoint + '/background-sync', {'repo': fixture['repo'], 'enabled': True, 'workspace_shared': True})
                post(endpoint + '/consent', {'repo': fixture['repo'], 'join': True, 'transcribe': True,
                    'notice_confirmed': True, 'record': False})
                sync = post(endpoint + '/background-sync', {'repo': fixture['repo'], 'enabled': True, 'workspace_shared': True})
                self.assertTrue(sync['enabled'])
                observed = get(endpoint + '?repo=' + fixture['repo'])
                self.assertTrue(observed['background_sync']['enabled'])
                self.assertEqual(observed['state'], 'planned')  # No provider write or attendance.
                disabled = post(endpoint + '/background-sync', {'repo': fixture['repo'], 'enabled': False})
                self.assertFalse(disabled['enabled'])

            finally:
                process.terminate()
                process.wait(timeout=10)
                process.stdout.close()
                process.stderr.close()


if __name__ == '__main__':
    unittest.main()
