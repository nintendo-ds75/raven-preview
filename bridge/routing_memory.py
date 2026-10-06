"""Learn first contacts from completed human conversations, not inferred authority."""
import hashlib
import json
import re
from .graph import now_iso, parse_facts


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS routing_feedback (
        decision_id TEXT NOT NULL, person_id TEXT NOT NULL, repo TEXT NOT NULL,
        question TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        facts TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(decision_id, person_id));
        CREATE INDEX IF NOT EXISTS routing_feedback_repo ON routing_feedback(repo,updated_at);''')
    db.executescript('''CREATE TABLE IF NOT EXISTS scope_clarifications (
        task_id TEXT NOT NULL, request_key TEXT NOT NULL, question TEXT NOT NULL,
        detail TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(task_id, request_key));''')


def record(graph, decision_id, person_id, outcome):
    d = graph.db.execute('SELECT * FROM decisions WHERE id=?', (decision_id,)).fetchone()
    if not d or not person_id:
        return
    if outcome == 'answered' and graph.db.execute(
            "SELECT 1 FROM events WHERE decision_id=? AND kind='route_learning_optout'", (decision_id,)).fetchone():
        return
    graph.db.execute('''INSERT INTO routing_feedback(decision_id,person_id,repo,question,category,path,facts,outcome,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(decision_id,person_id) DO UPDATE SET
        outcome=excluded.outcome, question=excluded.question, facts=excluded.facts, updated_at=excluded.updated_at''',
        (decision_id, person_id, d['repo'], d['question'], d['category'] or '', d['path'] or '',
         d['facts'] or '', outcome, now_iso()))
    graph._bump('')


def candidates(graph, repo, question, path='', context='', category='', facts=None):
    from .ladder import _meaningful_terms
    from .scopes import primary_scopes
    terms = set(_meaningful_terms(question))
    if not terms:
        return []
    stated = re.search(r'(?im)^facts?:\s*(.+)$', context or '')
    facts = parse_facts(facts) if facts is not None else parse_facts(stated[1] if stated else '')
    topics = set(primary_scopes(question, category))
    matches = []
    for row in graph.db.execute('''SELECT f.*,d.needs_review,d.owner_id,d.actor_basis FROM routing_feedback f
            JOIN decisions d ON d.id=f.decision_id WHERE f.repo=? ORDER BY f.updated_at DESC LIMIT 200''', (repo,)):
        prior = set(_meaningful_terms(row['question']))
        overlap = len(terms & prior) / max(1, len(terms | prior))
        if overlap < .5 or row['needs_review']:
            continue
        old_facts = parse_facts(row['facts'])
        if any(facts.get(k, '').lower() != v.lower() for k,v in old_facts.items()):
            continue
        old_topics = set(primary_scopes(row['question'], row['category']))
        if topics and old_topics and not topics & old_topics:
            continue
        if path not in ('', 'unknown') and row['path'] not in ('', 'unknown') and path != row['path']:
            continue
        person = graph.get_person(row['person_id'])
        if not person or not person['active'] or person['role'] == 'viewer':
            continue
        matches.append((person, row['outcome'], overlap, row['decision_id'], row['updated_at']))
    # A newer rejection wins over an earlier successful contact for the same topic.
    latest = {}
    for person, outcome, score, did, stamp in matches:
        latest.setdefault(person['id'], (person, outcome, score, did, stamp))
    return sorted(latest.values(), key=lambda r: (-r[2], r[0]['name']))


def request_key(question, client_ref='', context='', paths=()):
    return client_ref or hashlib.sha256(json.dumps([question.lower(), context, list(paths)],
                                                  sort_keys=True).encode()).hexdigest()[:24]


def clarify(graph, task_id, repo, question, key, path='', category='', facts=None):
    """Ask before crossing an unstated boundary of learned human routing.

    These historical values are possibilities, never facts inherited by a
    new task. Nothing is sent until the host supplies its actual scope.
    """
    from .ladder import _meaningful_terms
    from .scopes import primary_scopes
    from .store import Invalid
    previous = graph.db.execute("SELECT question FROM scope_clarifications WHERE task_id=? AND request_key=?",
                                (task_id, key)).fetchone()
    if previous and previous['question'] != question:
        raise Invalid('This client_ref already names a different scope clarification')
    facts = parse_facts(facts)
    # A matching unambiguous final contact is already enough; no need to
    # block it merely because another historical case was scoped.
    if any(outcome == 'answered' for _, outcome, *_ in candidates(
            graph, repo, question, path=path, category=category, facts=facts)):
        return None
    terms = set(_meaningful_terms(question))
    topics = set(primary_scopes(question, category))
    suggestions, seen = [], set()
    for row in graph.db.execute('''SELECT f.*, d.needs_review FROM routing_feedback f
            JOIN decisions d ON d.id=f.decision_id WHERE f.repo=? AND f.outcome='answered'
            ORDER BY f.updated_at DESC LIMIT 200''', (repo,)):
        if row['decision_id'] in seen or row['needs_review']:
            continue
        prior = set(_meaningful_terms(row['question']))
        if not terms or len(terms & prior) / max(1, len(terms | prior)) < .8:
            continue
        old_topics = set(primary_scopes(row['question'], row['category']))
        if topics and old_topics and not topics & old_topics:
            continue
        if path not in ('', 'unknown') and row['path'] not in ('', 'unknown') and path != row['path']:
            continue
        old = parse_facts(row['facts'])
        if any(k in facts and str(facts[k]).lower() != v.lower() for k, v in old.items()):
            continue  # explicitly different case: never use that scoped contact
        missing = {k: v for k, v in old.items() if k not in facts}
        person = graph.get_person(row['person_id'])
        if not missing or not person or not person['active'] or person['role'] == 'viewer':
            continue
        suggestions.append({'decision_id': row['decision_id'], 'prior_contact': person['name'],
                            'source_facts': old, 'missing_keys': sorted(missing)})
        seen.add(row['decision_id'])
        if len(suggestions) == 5:
            break
    if not suggestions:
        return None
    result = {'task_id': task_id, 'status': 'needs_scope_clarification', 'authorized': False,
              'blocking': True, 'request_key': key, 'question': question, 'scope_clarifications': suggestions,
              'next': 'Earlier answers and learned contacts depend on unstated scope. Confirm the actual missing '
                      'facts from this task or its requester, then retry bridge_add_node with the same client_ref '
                      'and explicit facts. Do not copy historical values as current truth. No question was sent.'}
    with graph.transaction():
        graph.db.execute('''INSERT INTO scope_clarifications(task_id,request_key,question,detail,created_at)
            VALUES(?,?,?,?,?) ON CONFLICT(task_id,request_key) DO UPDATE SET detail=excluded.detail''',
            (task_id, key, question, json.dumps(result), now_iso()))
    return result


def pending_scopes(graph, task_id):
    return [json.loads(row['detail']) for row in graph.db.execute(
        'SELECT detail FROM scope_clarifications WHERE task_id=? ORDER BY created_at,request_key', (task_id,))]


def resolve_scope(graph, task_id, key):
    graph.db.execute('DELETE FROM scope_clarifications WHERE task_id=? AND request_key=?', (task_id, key))
