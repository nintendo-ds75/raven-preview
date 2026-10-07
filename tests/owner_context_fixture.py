"""New synthetic UI data only; never reads the recovered customer screenshots."""
import json

from bridge import briefing, canvas
from bridge.config import Config


REQUEST = ("Review the retention settings for build-cache inspection records in synthetic/release-policy. "
           "The tracked item is WORK-42, for Cedar's preview environment. Find the documented limit and "
           "present the scoped recommendation to its reviewer. Keep this planning task available for a "
           "later handover; the work item should remain Open. This request does not include implementation "
           "or deployment. Show which source supports the recommendation, record the review outcome, "
           "and retain the work-item relationship so a future session can find the context.\n"
           "Original request end: <retain this exactly> & do not truncate.")
QUESTION = "What is the maximum retention period for Cedar preview build-cache inspection records?"


def seed(store):
    store.graph.set_setting('workspace_name', 'New synthetic UI · scope readability')
    owner = store.add_owner({'name': 'Synthetic Reviewer', 'team': 'Synthetic', 'patterns': 'src/release_policy/*'})
    repo = 'synthetic/release-policy'
    task = canvas.start_task(store, Config(model_api='none'), {
        'title': REQUEST[:300], 'goal': REQUEST, 'repo': repo, 'requester': 'Synthetic Requester',
        'paths': 'src/release_policy/specifiers.py',
        'facts': 'customer=Cedar,environment=preview,work_item=WORK-42'})['task_id']
    node = store.graph.add_decision(task, QUESTION, 'policy', 'pending', repo=repo,
        owner=owner['name'], path='src/release_policy/specifiers.py')
    context = ('External work item WORK-42 asks for the current maximum diagnostic-record retention duration. '
               'This is planning and fact-finding only: no implementation, launch or source change.\n'
               'Paths: src/release_policy/specifiers.py, src/release_policy/version.py')
    store.graph.db.execute('UPDATE decisions SET context=?,facts=?,scope_paths=? WHERE id=?', (
        context, json.dumps({'customer': 'Cedar', 'environment': 'preview', 'work_item': 'WORK-42'}),
        json.dumps(['src/release_policy/specifiers.py', 'src/release_policy/version.py']), node))
    source = store.add_record({'repo': repo, 'provider': 'generic', 'namespace': 'synthetic',
        'kind': 'doc', 'external_id': 'scope-fixture-policy', 'ref': 'SYNTHETIC-POLICY-1',
        'title': 'Synthetic retention policy', 'author': owner['name'], 'status': 'Current',
        'body': 'For Cedar in preview, retain build-cache inspection records for 21 calendar days after creation.'})['source']
    canvas.settle_node(store, {'task_id': task, 'node_id': node,
        'answer': '21 calendar days after creation for Cedar in preview.',
        'rationale': 'Synthetic source for a presentation regression.',
        'source_evidence': [{'record_id': source['record_id'], 'source_version_id': source['source_version_id'], 'role': 'support'}]})
    with store.graph.transaction():
        token = briefing.mint(store.graph, owner['person_id'], task, node)
    return {'task_id': task, 'node_id': node, 'token': token, 'source': source,
            'request': REQUEST, 'question': QUESTION, 'owner_id': owner['person_id']}
