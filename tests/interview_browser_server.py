"""Offline isolated server for browser-interview.cjs. No real microphone/provider."""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # noqa: F401
os.environ.update({'BRIDGE_MODEL_API': 'none', 'BRIDGE_SEMANTIC': '0', 'BRIDGE_LIVE': '0'})
from bridge.auth import Auth
from bridge import canvas
from bridge.config import Config
from bridge.server import make_server
from bridge.store import Store

store = Store(sys.argv[1])
store.graph.set_setting('workspace_name', 'Interview test workspace')
person = store.graph.add_person('Ada Owner', email='ada@example.test')
owner = store.add_owner({'name': 'Ada Owner', 'team': 'Runtime', 'patterns': '*'})
task = store.add_run({'title': 'Change timeout defaults', 'repo': 'org/runtime'})['id']
node = canvas.add_node(store, Config(model_api='none'), {'task_id': task,
        'question': 'Which timeout should new clients use?', 'context': 'Keep existing clients compatible.',
        'paths': ['src/client.py'], 'owner_id': owner['id']})['node_id']
auth = Auth(store, enabled=True)
server = make_server(store, port=0, auth=auth)
print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'cookie': auth.session_cookie(person).split(';')[0],
                  'task': task, 'node': node}), flush=True)
server.serve_forever()
