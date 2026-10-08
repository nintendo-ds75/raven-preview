"""Explicit, additive work-item context. Never evidence or authority.

Declarations are named facts, not identities extracted from prose. Reading
them never imports sources, invents pins, or repairs historical associations.
"""
import json

from . import context_memory as cm, source_lookup


def association(facts, anchors=(), sources=(), *, task_status=''):
    """Project an explicit declaration against already stored relationships."""
    if isinstance(facts, str):
        try:
            facts = json.loads(facts or '{}')
        except ValueError:
            facts = {}
    declared = facts.get('work_item', '') if isinstance(facts, dict) else ''
    declared = declared if isinstance(declared, str) else ''
    fields = ('record_id', 'source_version_id', 'provider', 'namespace', 'kind',
              'external_id', 'ref', 'role', 'current')
    links = [{**{key: source[key] for key in fields}, 'origin': origin,
              'relation': 'task_anchor' if origin == 'task' else 'decision_association'}
             for origin, items in (('task', anchors), ('decision', sources)) for source in items
             if source['role'] in ('work_item', 'context')]
    links.sort(key=lambda source: (source['origin'], source['record_id'], source['source_version_id'], source['role']))
    matches = [s for s in links if s['role'] == 'work_item'
               and declared and declared in (s['ref'], s['external_id'])]
    identities = {s['record_id'] for s in matches}
    status = ('undeclared' if not declared else 'unlinked' if not identities
              else 'linked' if len(identities) == 1 else 'ambiguous')
    historical = task_status in ('completed', 'abandoned')
    notice = {
        'undeclared': 'No structured work-item ID declared. Existing recorded links are shown separately; prose and client references are not links.',
        'unlinked': 'Declared work item is unlinked. Use bridge_lookup_record, bridge_get_record, then bridge_link_work_item with the exact canonical identity and current version.',
        'ambiguous': 'The declaration matches multiple explicitly linked records. Inspect their canonical identities; no single work item was selected.',
        'linked': 'Explicit work-item association only, never supporting evidence or approval. Recorded versions may be historical.',
    }[status]
    if historical and status in ('unlinked', 'ambiguous'):
        notice = ('Historical ' + status + ' declaration on a closed task. Preserve this recorded history; '
                  'start a new task for any new work-item association.')
    return {'declared': declared, 'status': status,
            'record_ids': sorted(identities), 'links': links, 'historical': historical, 'notice': notice}


def link(store, data):
    """Validate and write under the common SQLite/PG writer transaction."""
    from .store import Invalid
    required = {'task_id', 'repo', 'record_id', 'source_version_id', 'provider', 'namespace', 'object_kind'}
    allowed = required | {'external_id', 'ref', 'role'}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - allowed:
        raise Invalid('Link an existing work item with task_id, exact repo, record_id, source_version_id, '
                      'provider, namespace, object_kind, and optionally one of external_id or ref')
    for key in required:
        if not isinstance(data[key], str) or not data[key] or data[key] != data[key].strip():
            raise Invalid(f'{key} must be a nonempty exact key without surrounding whitespace')
    for key in ('task_id', 'record_id', 'source_version_id'):
        if len(data[key]) > 100:
            raise Invalid(f'{key} exceeds 100 characters')
    role = data.get('role', 'work_item')
    if role not in ('work_item', 'context'):
        raise Invalid('Work-item links are work_item or context, never supporting evidence or approval')
    query = {key: data[key] for key in ('repo', 'provider', 'namespace', 'object_kind', 'external_id', 'ref') if key in data}
    graph = store.graph
    with graph.transaction() as db:
        task = db.execute('SELECT repo,status FROM runs WHERE id=?', (data['task_id'],)).fetchone()
        if task is None or task['repo'] != data['repo']:
            raise Invalid('Work-item link is outside this task repository')
        if task['status'] in ('completed', 'abandoned'):
            raise Invalid('Closed task associations are historical; start a new task instead of rewriting them')
        if 'external_id' not in data and 'ref' not in data:
            selected = cm.citation(db, data['record_id'], data['source_version_id'])
            if selected is None or selected['repo'] != data['repo']:
                raise Invalid('Work-item record/version not found in this task repository')
            # The opaque ID selects one record. Reuse its exact stored identity
            # so lookup retains all canonical-key and live-availability checks.
            query['external_id'] = selected['external_id']
        resolved = source_lookup.lookup(db, **query)
        if resolved['status'] != 'matched':
            raise Invalid('Work-item identity is ' + resolved['status'] + '; resolve it with bridge_lookup_record before linking')
        if resolved['source']['record_id'] != data['record_id']:
            raise Invalid('Canonical identity does not match record_id; re-read the selected record')
        source = cm.citation(db, data['record_id'], data['source_version_id'])
        if (source is None or not source['current'] or source['availability'] != 'available'
                or resolved['latest_observed']['source_version_id'] != data['source_version_id']):
            raise Invalid('Work-item source changed or became unavailable; use bridge_get_record and retry with its current version')
        if (source['provider'], source['namespace'], source['kind']) != (data['provider'], data['namespace'], data['object_kind']):
            raise Invalid('Use the current canonical provider, namespace and object kind returned by bridge_get_record')
        # The first explicit selection establishes a namespace. Later links
        # cannot silently widen that provider to another installation. Other
        # providers may be deliberately attached as context, as import allows.
        # A native git record can acquire a GitHub canonical identity without
        # rewriting its source_records row. Use the exact anchored snapshot,
        # which is the namespace the caller actually selected.
        namespaces = {anchor['namespace'] for anchor in cm.anchors(db, data['task_id'])
                      if anchor['provider'] == source['provider']}
        if namespaces and source['namespace'] not in namespaces:
            raise Invalid('Work-item source is outside the explicitly selected task namespace; existing associations are retained')
        existing = db.execute('SELECT 1 FROM task_source_anchors WHERE task_id=? AND record_id=? '
                              'AND source_version_id=? AND role=?',
                              (data['task_id'], data['record_id'], data['source_version_id'], role)).fetchone()
        if not existing:
            cm.add_anchor(db, data['task_id'], data['record_id'], data['source_version_id'], role)
            graph._bump(data['repo'])
            graph.append_event('work_item_linked', {'task_id': data['task_id'], 'record_id': data['record_id'],
                'source_version_id': data['source_version_id'], 'role': role, 'provider': source['provider'],
                'namespace': source['namespace'], 'object_kind': source['kind'], 'external_id': source['external_id'],
                'ref': source['ref'], 'reason': 'Explicit task association, never supporting evidence or approval.'})
        return {'task_id': data['task_id'], 'changed': not bool(existing), 'relation': 'task_anchor',
                'source': {**source, 'role': role},
                'notice': 'Task association only. No decision evidence, signature, approval or existing dependency was changed.'}
