"""Fresh agent-proposal web fixture; synthetic records and local operator only."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.update(BRIDGE_MODEL_API='none', BRIDGE_SEMANTIC='0', BRIDGE_LIVE='0')
from bridge import briefing, canvas, context_memory as cm
from bridge.server import make_server
from bridge.store import Store

for method in ('complete', 'complete_json'):
    patch('bridge.llm.Client.' + method, side_effect=AssertionError('Provider calls forbidden')).start()
store = Store(sys.argv[1])
graph = store.graph
graph.set_setting('workspace_name', 'Fresh source browser fixture')
store.add_owner({'name':'Source Reviewer','team':'Policy','patterns':'policy/*'})
repo = 'synthetic/agent-source-browser'
task = store.add_run({'title':'Review a fresh source proposal','repo':repo})['id']
records = []
for number in range(1, 5):
    data = {'repo':repo,'kind':'jira','provider':'jira','namespace':'fixture.example',
        'ref':f'AGENT-{number}','title':f'Agent source {number}','author':'Source Writer','status':'Done',
        'body':f'SOURCE {number} COMPLETE START\n' + ('Context line with exact detail.\n' * 30)
            + f'<img src=x onerror="window.sourceExecuted=true"> SOURCE {number} COMPLETE END',
        'paths':['policy/archive.py'],'url':f'https://example.invalid/AGENT-{number}'}
    records.append({'data':data,'source':store.add_record(data)['source']})

def propose(question, pins):
    did = graph.add_decision(task,question,'policy','pending',repo=repo,owner='Source Reviewer',path='policy/archive.py')
    graph.db.execute('UPDATE decisions SET context=?,facts=?,scope_paths=? WHERE id=?',
        ('Applies only to this archive.',json.dumps({'customer':'SCOPE-CUSTOMER','release':'r17'}),
         json.dumps(['policy/archive.py','policy/export.py']),did))
    canvas.settle_node(store,{'task_id':task,'node_id':did,'answer':'Apply this complete source proposal to SCOPE-CUSTOMER release r17.',
        'source_evidence':pins})
    return did

support = propose('Apply both recorded constraints?', [cm.pin(r['source']) for r in records[:2]])
race = propose('Apply both constraints to the second archive?', [cm.pin(r['source']) for r in records[:2]])
context = propose('Which default should this archive use?', [cm.pin(records[2]['source'],'context')])
disclosure = propose('Review the exact source while navigating?', [cm.pin(records[3]['source'])])
with graph.transaction():
    person = graph.find_person('Source Reviewer')
    person_id = person['id'] if person else graph.add_person('Source Reviewer')
    links = {name:briefing.mint(graph,person_id,task,did) for name,did in [('support',support),('context',context),('disclosure',disclosure)]}
server = make_server(store,host='127.0.0.1',port=0)
print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}','task_id':task,'support':support,'race':race,
    'context':context,'disclosure':disclosure,'records':records,'links':links}),flush=True)
server.serve_forever()
