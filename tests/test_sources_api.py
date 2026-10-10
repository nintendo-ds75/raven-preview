"""The onboarding report and administration of native context sources on a
server with auth on: only an administrator adds, runs or disables a
source; agents and members read the report; the report reaches MCP,
connection status, /api/state and readiness."""
import os
from unittest.mock import patch
from urllib.error import HTTPError

from test_auth import BOOTSTRAP, SharedServer
from test_sources_docs import PAGE, Site


class SourcesAPITests(SharedServer):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {'JIRA_BASE_URL': '', 'SLACK_BOT_TOKEN': ''})
        env.start()
        self.addCleanup(env.stop)
        self.site = Site()
        self.addCleanup(self.site.close)
        self.site.page('/llms.txt', '- [Billing](/billing)\n', content_type='text/plain')
        self.site.page('/billing', PAGE, etag='"v1"')
        member = self.post('/api/people', {'name': 'Mia Member', 'role': 'member'}, token=BOOTSTRAP)
        self.member = self.post('/api/tokens', {'person_id': member['id'], 'kind': 'human'}, token=BOOTSTRAP)['token']
        self.agent = self.post('/api/tokens', {'person_id': member['id']}, token=BOOTSTRAP)['token']

    def body(self, **extra):
        return {'kind': 'docs', 'repo': 'example/service', 'target': self.site.origin + '/', 'workspace_shared': True,
                **extra}

    def test_only_an_administrator_adds_and_runs_a_source(self):
        self.assertEqual(self.status_of('POST', '/api/context/sources', self.body(), token=self.member), 403)
        self.assertEqual(self.status_of('POST', '/api/context/sources', self.body(), token=self.agent), 403)
        with self.assertRaises(HTTPError) as refused:
            self.post('/api/context/sources', self.body(workspace_shared=False), token=BOOTSTRAP)
        self.assertEqual(refused.exception.code, 400)
        source = self.post('/api/context/sources', self.body(), token=BOOTSTRAP)
        self.assertEqual(self.status_of('POST', f"/api/context/sources/{source['id']}/sync", {}, token=self.member), 403)
        result = self.post(f"/api/context/sources/{source['id']}/sync", {}, token=BOOTSTRAP)
        self.assertEqual(result['records'], 2)
        disabled = self.post(f"/api/context/sources/{source['id']}/disable", {}, token=BOOTSTRAP)
        self.assertFalse(disabled['enabled'])

    def test_members_and_agents_read_the_report_everywhere_it_appears(self):
        source = self.post('/api/context/sources', self.body(), token=BOOTSTRAP)
        self.post(f"/api/context/sources/{source['id']}/sync", {}, token=BOOTSTRAP)
        report = self.get('/api/context/sources', token=self.member)
        [entry] = report['sources']
        self.assertEqual((entry['kind'], entry['state'], entry['records']), ('docs', 'current', 2))
        missing = {s['kind']: s for s in report['not_configured']}
        self.assertEqual(set(missing), {'slack', 'jira'})
        self.assertFalse(missing['jira']['credentials_present'])
        self.assertIn('--workspace-shared', missing['slack']['enable'])
        mcp = self.mcp('bridge_context_sources', {}, self.agent)
        self.assertFalse(mcp['isError'])
        self.assertEqual(mcp['result']['sources'][0]['records'], 2)
        status = self.mcp('bridge_connection_status', {}, self.agent)['result']
        self.assertEqual(status['context_sources']['sources'][0]['id'], source['id'])
        self.assertEqual(self.get('/api/state', token=self.member)['context_sources']['sources'][0]['id'], source['id'])

    def test_a_failing_source_appears_in_readiness(self):
        source = self.post('/api/context/sources', self.body(), token=BOOTSTRAP)
        self.site.routes['/billing'] = (500, 'down', {})
        with self.assertRaises(HTTPError):
            self.post(f"/api/context/sources/{source['id']}/sync", {}, token=BOOTSTRAP)
        readiness = self.get('/api/state', token=self.member)['readiness']
        self.assertTrue(any(r['key'] == 'context_source_' + source['id'] for r in readiness))

    def test_the_report_tool_is_read_only_for_agents(self):
        from bridge.mcp import READ_ONLY_TOOLS
        self.assertIn('bridge_context_sources', READ_ONLY_TOOLS)
