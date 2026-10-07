"""Synthetic local-operator source revalidation fixture, with no provider calls."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_LIVE='0')
from bridge import canvas, ladder, briefing
from bridge.graph import ts_to_iso
from bridge.server import make_server
from bridge.store import Store

store = Store(sys.argv[1])
store.graph.set_setting('workspace_name', 'Source review browser fixture')
store.add_owner({'name': 'Ada Example', 'team': 'Policy', 'patterns': 'policy/*'})
repo = 'synthetic/context-browser'
run = store.add_run({'title': 'Review changed retention source', 'repo': repo})['id']
source_data = {'repo': repo, 'kind': 'jira', 'ref': 'POL-1', 'title': 'Retention policy',
               'body': 'Keep records for thirty days.', 'status': 'Done', 'paths': ['policy/retention.py']}
source = store.add_record(source_data)
graph = store.graph
row = graph.intents_by_ref(['POL-1'], repo)[0]
did = graph.add_decision(run, 'How many days should records remain?', 'policy', 'pending',
                         repo=repo, owner='Ada Example', path='policy/retention.py')
graph.publish_evidence(did, [row], answer='Keep records for thirty days.', source='record',
                       status='resolved', kind='evidence', signoff='required')
canvas.sign_off(store, did, {'by': 'Ada Example', 'expected_updated_at': store.get_decision(did)['updated_at']})
store.add_record({**source_data, 'body': 'Revision B: keep records for thirty days after migration.'})
human_run = store.add_run({'title': 'Review changed human premise', 'repo': repo})['id']
parent = graph.add_decision(human_run, 'What is the Northstar release 4 retention constraint?', 'policy', 'pending',
                            repo=repo, owner='Ada Example', path='policy/northstar.py')
graph.db.execute('UPDATE decisions SET facts=?,scope_paths=?,context=? WHERE id=?',
                 ('{"customer":"Northstar","release":"4"}', '["policy/northstar.py"]', 'Release 4 only', parent))
store.answer(parent, {'answer': 'Thirty days.', 'rationale': 'Northstar contract limitation'})
child_run = store.add_run({'title': 'Apply the human premise', 'repo': repo})['id']
human_child = graph.add_decision(child_run, 'What should the archive apply?', 'policy', 'pending',
                                repo=repo, owner='Ada Example', path='policy/northstar.py')
prior = graph.get_decision(parent)
graph.publish_evidence(human_child, [ladder._as_record(prior)], answer='Thirty days.', source='memory',
                       source_id=parent, source_revision=ts_to_iso(prior.updated_at),
                       status='resolved', kind='evidence', signoff='required')
canvas.sign_off(store, human_child, {'by': 'Ada Example', 'expected_updated_at': store.get_decision(human_child)['updated_at']})
store.answer(parent, {'answer': 'Seven days.', 'rationale': 'Northstar contract limitation',
                      'expected_updated_at': store.get_decision(parent)['updated_at']})
server = make_server(store, host='127.0.0.1', port=0)
with graph.transaction():
    person = graph.find_person('Ada Example')
    person_id = person['id'] if person else graph.add_person('Ada Example', slack_id='UEXAMPLE')
    personal_link = briefing.mint(graph, person_id, run, did)
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'decision_id': did, 'task_id': run, 'source_data': source_data, 'human_child': human_child, 'human_parent': parent, 'personal_link': personal_link}), flush=True)
server.serve_forever()
