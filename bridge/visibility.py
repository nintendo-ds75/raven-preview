"""Current access at outbound boundaries, including copied task history.

The database keeps historical evidence and signatures. Losing source access does
not erase that audit record or make it safe to send to a reader or model. A task
which already read a blocked source is withheld as a whole: we cannot reliably
separate quotes from paraphrases in its transcript, patch or saved host context.
"""
import re

from . import context_connectors
from .store import Invalid

NOTICE = 'Content withheld because connected source access is not current. Revalidate access before reading or resuming this task.'


def affected(db):
    predicate, args = context_connectors.freshness_sql()
    predicate = "(s.availability='available' AND " + predicate + ')'
    records = {r['id'] for r in db.execute('SELECT s.id FROM source_records s WHERE NOT ' + predicate, args)}
    withheld = {r['source_version_id'] for r in db.execute('SELECT source_version_id FROM withheld_source_versions')}
    if not records and not withheld:
        return set(), set()
    # Include historical edges: a replacement can make an answer usable again,
    # but the old transcript and patch may still contain the restricted content.
    nodes = {r['id'] for r in db.execute('WITH RECURSIVE blocked(id) AS ('
        'SELECT e.decision_id FROM decision_source_edges e JOIN source_records s ON s.id=e.record_id WHERE NOT '
        + predicate + ' OR EXISTS (SELECT 1 FROM withheld_source_versions w WHERE w.source_version_id=e.source_version_id) UNION SELECT links.decision_id FROM ('
        "SELECT id AS decision_id,source_id AS related_id FROM decisions WHERE coalesce(source_id,'')<>'' "
        "UNION SELECT id,parent_id FROM decisions WHERE parent_id<>'' "
        "UNION SELECT decision_id,related_id FROM decision_links WHERE kind IN ('derived','depends')"
        ') links JOIN blocked b ON b.id=links.related_id) SELECT id FROM blocked', args)}
    tasks = {r['task_id'] for r in db.execute('SELECT a.task_id FROM task_source_anchors a '
        'JOIN source_records s ON s.id=a.record_id WHERE NOT ' + predicate, args)}
    tasks.update(r['task_id'] for r in db.execute('SELECT a.task_id FROM task_source_reads a '
        'JOIN source_records s ON s.id=a.record_id WHERE NOT ' + predicate, args))
    tasks.update(r['task_id'] for r in db.execute('SELECT a.task_id FROM task_source_reads a '
        'JOIN withheld_source_versions w ON w.source_version_id=a.source_version_id'))
    tasks.update(r['task_id'] for r in db.execute('SELECT a.task_id FROM task_source_anchors a '
        'JOIN withheld_source_versions w ON w.source_version_id=a.source_version_id'))
    # Avoid large IN statements and keep the traversal compatible with both DBs.
    decision_tasks = list(db.execute('SELECT id,run_id FROM decisions'))
    reads = list(db.execute('SELECT task_id,decision_id FROM task_context_reads'))
    while True:
        # Reading any decision also copies its task's disclosure boundary,
        # including work-item anchors and prior reads, not just support edges.
        nodes.update(r['id'] for r in decision_tasks if r['run_id'] in tasks)
        inherited = {r['run_id'] for r in decision_tasks if r['id'] in nodes}
        inherited.update(r['task_id'] for r in reads if r['decision_id'] in nodes)
        if inherited <= tasks:
            break
        tasks.update(inherited)
    versions = {r['id'] for r in db.execute('SELECT id,record_id FROM source_versions') if r['record_id'] in records}
    return records | versions | withheld | nodes | tasks, tasks


def require_task(store, task_id):
    if task_id in affected(store.graph.db)[1]:
        raise Invalid(NOTICE)


def remember_reads(store, task_id, value):
    """Record exposed evidence, including local search hits and prior answers."""
    from .source_access import stamp
    db = store.graph.db
    run = db.execute('SELECT repo FROM runs WHERE id=?', (task_id,)).fetchone()
    if not run:
        return
    ids, versions = set(), {}
    def scan(obj):
        if isinstance(obj, dict):
            if isinstance(obj.get('record_id'), str) and isinstance(obj.get('source_version_id'), str):
                versions[obj['record_id']] = obj['source_version_id']
            for k, v in obj.items():
                if k in ('id', 'record_id', 'source_id', 'node_id', 'decision_id') and isinstance(v, str):
                    ids.add(v)
                else:
                    scan(v)
        elif isinstance(obj, list):
            for v in obj:
                scan(v)
    scan(value)
    with store.graph.transaction():
        for ident in ids:
            # Reading is not a work-item association or a new retrieval scope.
            version = versions.get(ident)
            source = db.execute('SELECT s.id FROM source_records s JOIN source_versions v ON v.record_id=s.id '
                                'WHERE s.id=? AND s.repo=? AND v.id=?', (ident, run['repo'], version)).fetchone() if version else None
            if source:
                db.execute('INSERT INTO task_source_reads VALUES(?,?,?,?) ON CONFLICT(task_id,record_id,source_version_id) DO NOTHING',
                           (task_id, ident, version, stamp()))
            decision = db.execute('SELECT d.id,d.updated_at FROM decisions d JOIN runs r ON r.id=d.run_id '
                                  'WHERE d.id=? AND r.repo=?', (ident, run['repo'])).fetchone()
            if decision:
                db.execute('INSERT INTO task_context_reads VALUES(?,?,?,?) ON CONFLICT(task_id,decision_id,revision) DO NOTHING',
                           (task_id, ident, decision['updated_at'], stamp()))


def public(store, value, *, denied=None):
    """Redact whole identified objects, never substring-rewrite source claims."""
    denied = affected(store.graph.db)[0] if denied is None else denied
    if not denied:
        return value
    keys = {'id', 'node_id', 'decision_id', 'task_id', 'run_id', 'record_id', 'source_version_id', 'source_id'}
    pattern = re.compile(r'(?<![A-Za-z0-9_])(?:' + '|'.join(re.escape(i) for i in denied) + r')(?![A-Za-z0-9_])')

    def visit(obj):
        if isinstance(obj, list):
            return [visit(item) for item in obj]
        if isinstance(obj, dict):
            if any(isinstance(obj.get(k), str) and obj[k] in denied for k in keys):
                identifiers = {k: obj[k] for k in keys if isinstance(obj.get(k), str)}
                return {**identifiers, 'withheld': True, 'notice': NOTICE, 'title': 'Access needs attention',
                        'prompt': 'Access needs attention', 'summary': NOTICE,
                        'question': 'Content unavailable', 'status': 'unavailable', 'kind': 'withheld',
                        'authorized': False, 'blocking': True, 'needs_review': True, 'nodes': [], 'next': NOTICE}
            return {k: visit(v) for k, v in obj.items()}
        if isinstance(obj, str) and pattern.search(obj):
            # Includes JSON encoded event detail and old tool receipts.
            return NOTICE
        return obj
    return visit(value)
