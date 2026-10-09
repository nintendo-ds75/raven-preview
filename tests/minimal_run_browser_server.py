"""Fresh synthetic minimal-run fixture: actual local Store/HTTP, no providers."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # Sanitizes deployment credentials/settings before constructing the Store.
from bridge import canvas
from bridge.authz import Actor
from bridge.config import Config
from bridge.server import make_server
from bridge.store import Store


def seed(store):
    store.graph.set_setting('workspace_name', 'Fresh synthetic minimal-run QA')
    reviewer = store.add_owner({'name': 'Synthetic Minimal Reviewer', 'team': 'Synthetic',
                                'patterns': 'src/minimal/*'})
    repo = 'synthetic/minimal-ui'
    store.add_record({'repo': repo, 'provider': 'generic', 'namespace': 'synthetic-minimal',
                      'kind': 'doc', 'external_id': 'synthetic-minimal-policy',
                      'ref': 'SYNTHETIC-MINIMAL-1', 'title': 'Synthetic retention policy',
                      'author': reviewer['name'], 'body': 'Retain synthetic preview records for 21 days.'})
    tasks = []
    for ordinal, count in ((1, 28), (2, 2)):
        request = f'Synthetic minimal run {ordinal}: check the preview retention policy.'
        task_id = canvas.start_task(store, Config(model_api='none'), {'title': request,
            'goal': request + ' Keep the complete request and its selected contact.',
            'repo': repo, 'agent': 'Synthetic QA agent', 'requester': 'Synthetic Requester',
            'paths': 'src/minimal/policy.py'})['task_id']
        nodes = []
        for index in range(count):
            node_id = store.graph.add_decision(task_id, f'Synthetic question {index + 1}: keep the preview limit?',
                'policy', 'pending', repo=repo, owner=reviewer['name'], path='src/minimal/policy.py',
                owner_evidence='Recorded synthetic CODEOWNERS match src/minimal/*',
                context='A synthetic question; no external provider or production source.')
            nodes.append(node_id)
        for index in range(30 if ordinal == 1 else 1):
            canvas.add_note(store, task_id, {'by': 'Synthetic Author', 'text': f'Synthetic note {index + 1}'})
        store.graph.append_event('owner_changed', {'task_id': task_id, 'decision_id': nodes[0],
            'by': 'Synthetic First Reviewer', 'referral': True, 'to': reviewer['name'],
            'why': 'Synthetic recorded referral to the policy reviewer'})
        tasks.append({'task_id': task_id, 'node_ids': nodes, 'goal': request})
    earlier_task = canvas.start_task(store, Config(model_api='none'), {
        'title': 'Earlier synthetic support-copy decision', 'repo': repo,
        'agent': 'Synthetic QA agent', 'paths': 'src/minimal/export.py'})['task_id']
    earlier_decision = store.graph.add_decision(earlier_task,
        'How long could support keep an export copy in the earlier preview?',
        'policy', 'pending', repo=repo, owner=reviewer['name'], path='src/minimal/export.py',
        context='An earlier synthetic scope, retained only as context for a new decision.')
    store.answer(earlier_decision, {
        'answer': 'Support copies may be retained for seven days within the earlier preview scope.',
        'rationale': 'Synthetic historical policy decision.'},
        actor=Actor.person(store.graph.get_person(reviewer['person_id'])))
    return {'tasks': tasks, 'repo': repo, 'earlier_decision_id': earlier_decision,
            'fixture': 'Fresh synthetic 2026-10-09; not recovered prior UI'}


if __name__ == '__main__':
    os.environ['BRIDGE_MODEL_API'] = 'none'
    for target in ('bridge.llm.Client.complete', 'bridge.llm.Client.complete_json',
                   'bridge.github.GitHubAPI.get', 'urllib.request.urlopen'):
        patch(target, side_effect=AssertionError('External provider calls forbidden in minimal UI fixture')).start()
    store = Store(sys.argv[1])
    assert not store.delivery.enabled
    fixture = seed(store)
    server = make_server(store, host='127.0.0.1', port=0)
    print(json.dumps({**fixture, 'url': f'http://127.0.0.1:{server.server_port}'}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        store.graph.close()
