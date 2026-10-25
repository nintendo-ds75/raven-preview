"""Real local UI/API fixture; synthetic evidence, no external providers or workers.

Run with a fresh SQLite DB path. Prints its URL and IDs, then serves until stopped.
No model/harness/meeting network request is permitted in this fixture.
"""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # noqa: E402,F401

for key in tuple(os.environ):
    if key.startswith(('RAVEN_HOST_', 'VEXA_', 'RAVEN_VEXA_')) or key in ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY'):
        os.environ.pop(key, None)
os.environ.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_LIVE='0')
from bridge import objectives, meeting_records  # noqa: E402
from bridge.auth import Identity  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.server import make_server  # noqa: E402
from bridge.store import Store  # noqa: E402
from bridge.workbench import Workbench  # noqa: E402

for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json',
               'bridge.github.GitHubAPI.get', 'bridge.meeting_providers.Vexa._request'):
    patch(target, side_effect=AssertionError('External calls forbidden in synthetic UI fixture')).start()
store = Store(sys.argv[1])
store.graph.set_setting('workspace_name', 'Raven startup OS · synthetic QA')
actor = Identity('fixture-admin', 'Fixture operator', 'admin', 'session')
repo = 'synthetic/billing'
store.add_owner({'name': 'Fixture operator', 'team': 'Synthetic', 'patterns': '*'})
meeting = meeting_records.create(store, {'repo': repo, 'provider': 'import', 'namespace': 'synthetic-calls',
    'platform': 'import', 'native_meeting_id': 'onboarding-call', 'title': 'Synthetic onboarding call'})
meeting_records.import_transcript(store, meeting['id'], repo, {'body': 'Synthetic customer asks which trial limit applies. Product owner says to verify the signed policy before changing the copy.',
    'source_version': 'fixture-v1', 'workspace_shared': True, 'by': actor.name})
attendance = meeting_records.create(store, {'repo': repo, 'provider': 'vexa', 'namespace': 'synthetic-calls',
    'platform': 'google_meet', 'native_meeting_id': 'fixture-only-no-join', 'title': 'Synthetic consent-only attendance plan'})
meeting = meeting_records.get(store, meeting['id'], repo)
source = store.get_record(meeting['record_id'], repo)['source']
payload = {'title': 'Make the billing release dependable', 'outcome': 'Prove the changed usage rule and prepare a reviewable patch.',
    'repository': repo, 'provider': 'claude', 'success_criteria': 'The independent billing regression passes on the exact patch.',
    'stop_criteria': 'Stop if pricing policy is unclear or credentials are needed.',
    'guardrails': 'Disposable checkout only. No production access, push or deployment.'}
objective = objectives.create(store, {**payload, 'request_key': 'fixture-objective', 'source_ids': [source['record_id']]}, actor)
objectives.act(store, objective['id'], 'checkpoint', {'expected_revision': objective['revision'], 'request_key': 'fixture-checkpoint',
    'summary': 'The regression case is isolated. Waiting for the policy owner.', 'waiting_on': 'Pricing policy sign-off',
    'next_check_at': '2026-10-26T12:00:00+00:00'}, actor)
proposal = objectives.propose(store, {**payload, 'title': 'Close the onboarding question from the call',
    'outcome': 'Investigate which trial limit applies before changing customer-facing copy.',
    'why_now': 'The synthetic call revealed a policy ambiguity.', 'judgment_kind': 'interpretation_tradeoff',
    'cases': {}}, [{'record_id': source['record_id'], 'source_version_id': source['source_version_id']}])
workbench = Workbench(store, [{'id': repo, 'path': str(Path(__file__).resolve().parents[1] / 'fixtures' / 'billing')}],
    Path(sys.argv[1]).parent / 'objective-fixture-copies', cfg=Config(model_api='none'))
server = make_server(store, host='127.0.0.1', port=0, workbench=workbench)
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/#today', 'repo': repo, 'objective_id': objective['id'],
    'proposal_id': proposal['id'], 'meeting_id': meeting['id'], 'attendance_id': attendance['id'], 'live_providers': False}), flush=True)
server.serve_forever()
