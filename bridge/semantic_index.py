"""Local learned retrieval over eligible rows in the existing evidence graph.

Vectors nominate evidence, never grant permission. Every search reconstructs
eligibility before looking at cached vectors; withdrawn or out-of-scope rows
cannot leak back through an older index entry. The legacy lexical index stays
available when the local model is disabled or unavailable.
"""
import hashlib
import json
import math
import os
import threading

MODEL = 'BAAI/bge-small-en-v1.5'
_lock = threading.RLock()
_models = {}


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS semantic_vectors (
        model TEXT NOT NULL, content_hash TEXT NOT NULL, vector TEXT NOT NULL,
        PRIMARY KEY(model, content_hash));''')


def enabled():
    return (os.environ.get('BRIDGE_EMBEDDINGS', 'local') == 'local'
            and os.environ.get('BRIDGE_SEMANTIC') != '0')


def _model():
    from fastembed import TextEmbedding
    name = os.environ.get('BRIDGE_EMBEDDING_MODEL', MODEL)
    with _lock:
        if name not in _models:
            _models[name] = TextEmbedding(model_name=name, threads=2,
                                         cache_dir=os.environ.get('FASTEMBED_CACHE_PATH') or None)
        return name, _models[name]


def _normal(vector):
    values = [float(v) for v in vector]
    if not values or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid embedding')
    norm = math.sqrt(sum(v * v for v in values))
    if not norm:
        raise ValueError('Empty embedding')
    return [v / norm for v in values]


def rank(graph, query, documents, limit=16):
    """Return id/score candidates and observable availability; no model calls
    under a database write lock. Documents are already access/scope filtered.
    Chunk long records so a relevant section after the model's input window
    can be found. Cache content hashes, not mutable record identifiers.
    """
    if not enabled() or not documents:
        return [], {'backend': 'disabled', 'indexed': 0}
    try:
        name, model = _model()
        pieces = []
        for key, text in documents:
            text = str(text)
            for offset in range(0, max(1, len(text)), 1200):
                chunk = text[offset:offset + 1600]
                pieces.append((key, hashlib.sha256(chunk.encode()).hexdigest(), chunk))
        cached = {}
        hashes = list(dict.fromkeys(p[1] for p in pieces))
        for offset in range(0, len(hashes), 300):
            part = hashes[offset:offset + 300]
            for row in graph.db.execute('SELECT content_hash,vector FROM semantic_vectors WHERE model=? '
                                       'AND content_hash IN (' + ','.join('?' for _ in part) + ')', [name, *part]):
                cached[row['content_hash']] = _normal(json.loads(row['vector']))
        missing = {digest: text for _, digest, text in pieces if digest not in cached}
        for offset in range(0, len(missing), 32):
            batch = list(missing.items())[offset:offset + 32]
            with _lock:
                vectors = list(model.passage_embed([text for _, text in batch]))
            if len(vectors) != len(batch):
                raise ValueError('Incomplete embedding batch')
            rows = [(name, digest, json.dumps(_normal(vector))) for (digest, _), vector in zip(batch, vectors)]
            with graph.transaction():
                graph.db.executemany('INSERT OR IGNORE INTO semantic_vectors(model,content_hash,vector) VALUES(?,?,?)', rows)
            cached.update({digest: json.loads(vector) for _, digest, vector in rows})
        with _lock:
            q = _normal(next(iter(model.query_embed(query))))
        scores = {}
        for key, digest, _ in pieces:
            vector = cached[digest]
            if len(vector) != len(q):
                raise ValueError('Embedding dimension mismatch')
            score = sum(a * b for a, b in zip(q, vector))
            scores[key] = max(scores.get(key, -1), score)
        return sorted(scores.items(), key=lambda p: (-p[1], p[0]))[:limit], {
            'backend': 'local', 'model': name, 'indexed': len(documents), 'chunks': len(pieces)}
    except Exception as error:
        return [], {'backend': 'unavailable', 'indexed': 0, 'error': type(error).__name__,
                    'next': 'Check the local embedding model installation/cache; lexical retrieval remains available.'}


def sources(graph, query, repo, limit=16):
    from .ladder import _meaningful_terms
    rows = {str(r['id']): r for r, _ in graph._intent_corpus(repo)[0]}
    lexical = list(graph.intents_matching(_meaningful_terms(query), limit=limit, repo=repo))
    learned, diagnostic = rank(graph, query, [(key, r['title'] + '\n' + r['body']) for key, r in rows.items()], limit)
    # Reciprocal rank fusion preserves exact/lexical recall without assuming
    # learned cosine and lexical scores measure the same thing.
    scores = {}
    for ordering in ([str(r['id']) for r in lexical], [key for key, score in learned if score >= 0.35]):
        for i, key in enumerate(ordering):
            scores[key] = scores.get(key, 0) + 1 / (60 + i + 1)
    # Re-read eligibility after inference in case access changed meanwhile.
    current = {str(r['id']): r for r, _ in graph._intent_corpus(repo)[0]}
    result = [current[key] for key in sorted(scores, key=lambda k: (-scores[k], k)) if key in current][:limit]
    return result, diagnostic


def memories(graph, query, repo, limit=12):
    from .graph import MEMORY_STATUSES
    rows = graph._memory_rows(MEMORY_STATUSES, repo)
    nominated, diagnostic = rank(graph, query, [(r['id'], r['question'] + '\n' + (r['answer'] or '')) for r in rows], limit)
    current = {r['id']: r for r in graph._memory_rows(MEMORY_STATUSES, repo, {key for key, _ in nominated})} if nominated else {}
    return [(key, score) for key, score in nominated if key in current and score >= 0.35], diagnostic
