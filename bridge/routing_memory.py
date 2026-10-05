"""Learn first contacts from completed human conversations, not inferred authority."""
import re
from .graph import now_iso, parse_facts


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS routing_feedback (
        decision_id TEXT NOT NULL, person_id TEXT NOT NULL, repo TEXT NOT NULL,
        question TEXT NOT NULL, category TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        facts TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(decision_id, person_id));
        CREATE INDEX IF NOT EXISTS routing_feedback_repo ON routing_feedback(repo,updated_at);''')


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
