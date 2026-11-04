"""Provider-free server for the bounded owner-context presentation regression."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # noqa: F401  (puts the repository root on sys.path)
from raven.server import make_server
from raven.store import Store
from owner_context_fixture import seed

for target in ('raven.llm.Client.complete', 'raven.llm.Client.complete_json', 'raven.github.GitHubAPI.get'):
    patch(target, side_effect=AssertionError('External calls forbidden')).start()
store = Store(sys.argv[1])
assert not store.delivery.enabled
fixture = seed(store)
server = make_server(store, host='127.0.0.1', port=0)
print(json.dumps({**fixture, 'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
server.serve_forever()
