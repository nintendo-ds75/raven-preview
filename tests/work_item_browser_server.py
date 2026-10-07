"""Local work-item context fixture. No external services, signing or new grants."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_LIVE='0')
from bridge import canvas
from bridge.config import Config
from bridge.server import make_server
from bridge.store import Store

for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json', 'bridge.github.GitHubAPI.get'):
    patch(target, side_effect=AssertionError('External calls forbidden')).start()
store = Store(sys.argv[1])
assert not store.delivery.enabled
store.graph.set_setting('workspace_name', 'Work-item context browser fixture')
store.add_owner({'name': 'Context Owner', 'team': 'Synthetic', 'patterns': 'policy/*'})
repo = 'synthetic/work-item-browser'
config = Config(model_api='none')
task = canvas.start_task(store, config, {'title': 'Update the synthetic archive policy', 'repo': repo,
    'paths': 'policy/archive.py', 'facts': 'work_item=CASE-1'})['task_id']
node = canvas.add_node(store, config, {'task_id': task, 'question': 'How long should this archive remain?',
    'paths': 'policy/archive.py', 'category': 'policy', 'facts': 'work_item=CASE-1'})['node_id']
canvas.settle_node(store, {'task_id': task, 'node_id': node,
    'answer': 'Propose thirty days for the synthetic archive.', 'rationale': 'Unsigned proposal for review.'})
source_data = {'repo': repo, 'provider': 'jira', 'namespace': 'synthetic-site-a', 'kind': 'jira',
    'external_id': 'object-1', 'ref': 'CASE-1', 'title': 'Synthetic archive work item',
    'body': 'Original work-item context.', 'status': 'Open'}
source = store.add_record(source_data)['source']
decision_task = canvas.start_task(store, config, {'title': 'Decision-only work-item association', 'repo': repo,
    'facts': 'work_item=CASE-1'})['task_id']
decision_node = store.graph.add_decision(decision_task, 'Which decision-local archive setting?', 'policy',
    'pending', repo=repo, owner='Context Owner', path='policy/archive.py')
store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps({'work_item':'CASE-1'}), decision_node))
canvas.settle_node(store, {'task_id':decision_task, 'node_id':decision_node, 'answer':'An unsigned decision-level proposal.',
    'source_evidence':[{'record_id':source['record_id'], 'source_version_id':source['source_version_id'], 'role':'work_item'}]})
ambiguous_task = canvas.start_task(store, config, {'title': 'Ambiguous work-item associations', 'repo':repo,
    'facts':'work_item=CASE-1'})['task_id']
ambiguous_node = store.graph.add_decision(ambiguous_task, 'Which linked issue defines this separate archive?', 'policy',
    'pending', repo=repo, owner='Context Owner', path='policy/archive.py')
store.graph.db.execute('UPDATE decisions SET facts=? WHERE id=?', (json.dumps({'work_item':'CASE-1'}), ambiguous_node))
other = store.add_record({**source_data, 'external_id':'object-2', 'title':'Another issue with the same display ref'})['source']
for item in (source, other):
    store.link_work_item({'task_id':ambiguous_task, 'repo':repo, 'provider':item['provider'], 'namespace':item['namespace'],
        'object_kind':item['kind'], 'external_id':item['external_id'], 'record_id':item['record_id'],
        'source_version_id':item['source_version_id']})
closed_task = canvas.start_task(store, config, {'title':'Historical task with an unlinked declaration', 'repo':repo,
    'facts':'work_item=CASE-CLOSED'})['task_id']
store.update_run(closed_task, {'status':'completed'})
if '--render-payloads' in sys.argv:
    print(json.dumps({'decision_only':store.get_decision(decision_node),
        'ambiguous_node':canvas.get_tree(store, ambiguous_task)['nodes'][0],
        'historical_task':canvas.get_tree(store, closed_task)}), flush=True)
    store.graph.close()
    sys.exit(0)
server = make_server(store, host='127.0.0.1', port=0)
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'repo': repo, 'task_id': task,
    'node_id': node, 'source': source, 'source_data': source_data,
    'decision_task':decision_task, 'decision_node':decision_node, 'ambiguous_task':ambiguous_task,
    'ambiguous_node':ambiguous_node, 'ambiguous_sources':[source,other], 'closed_task':closed_task}), flush=True)
server.serve_forever()
