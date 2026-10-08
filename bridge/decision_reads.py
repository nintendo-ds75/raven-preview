"""Bounded, read-only MCP discovery of current decisions and saved history.

This module does not nominate reuse candidates or calculate authorization.
Current authorization comes from Store; saved states are descriptive only.
"""
import base64
import binascii
import hashlib
import json
import re

from .store import Invalid

MAX_CONTENT_BYTES = 32 * 1024
DEFAULT_LIMIT = 10
MAX_LIMIT = 50
HISTORY_COLLECTIONS = ('context_history', 'historical_approvals')
NOTICE = (
    'Current fields describe the decision now. Historical summaries describe saved states, never current '
    'authorization or permission for a new request. Read relevant saved versions and linked decisions before '
    'claiming there was no approval or only one prior decision. Previews and omitted fields are incomplete '
    'evidence; use their exact-version or full-detail reads. History counts measure saved snapshots, not distinct '
    'human approval actions; repeated recorded states are not additional signatures or grants. '
    'Human HTTP review retains the complete record.'
)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _size(value):
    # Include JSON escaping of the MCP text, as the transport does.
    return len(json.dumps(json.dumps(value)).encode())


def _begin(db):
    db.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY'
               if getattr(db, 'dialect', '') == 'postgres' else 'BEGIN')


def _limit(args):
    value = args.get('limit', DEFAULT_LIMIT)
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise Invalid(f'limit must be an integer from 1 to {MAX_LIMIT}')
    return value


def _cursor(binding, snapshot, offsets):
    data = {'v': 1, 'binding': binding, 'snapshot': snapshot, 'offsets': offsets}
    return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).decode().rstrip('=')


def _read_cursor(value, binding, keys):
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        data = json.loads(base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True))
        if (not isinstance(data, dict) or set(data) != {'v', 'binding', 'snapshot', 'offsets'}
                or type(data['v']) is not int or data['v'] != 1
                or not isinstance(data['snapshot'], str) or len(data['snapshot']) != 64
                or not isinstance(data['offsets'], dict) or set(data['offsets']) != set(keys)
                or any(type(n) is not int or n < 0 for n in data['offsets'].values())):
            raise ValueError
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError):
        raise Invalid('Invalid decision discovery cursor; restart without cursor') from None
    if data['binding'] != binding:
        raise Invalid('Decision discovery cursor belongs to different arguments; repeat the original arguments or restart')
    return data


def _saved(db, field):
    """Only constant application-owned field names enter these expressions."""
    if getattr(db, 'dialect', '') == 'postgres':
        return "coalesce(v.snapshot::jsonb #>> '{decision," + field + "}', '')"
    return "coalesce(CAST(json_extract(v.snapshot, '$.decision." + field + "') AS TEXT), '')"


def _approval_predicate(db):
    # Recorded status only: no signature validation or new eligibility logic.
    return (f"({_saved(db, 'status')}='approved' OR {_saved(db, 'signoff')} IN ('signed','rule')) "
            f"AND {_saved(db, 'needs_review')} IN ('','0','false')")


def _version_columns(db):
    fields = ('status', 'signoff', 'needs_review', 'signed_by', 'answered_by', 'source_id',
              'source_revision', 'reusable', 'rule_scope', 'rule_conditions', 'rule_expires', 'rule_ended_at')
    columns = ['v.id', 'v.sequence', 'v.recorded_at']
    # Large approval terms stay in the exact snapshot, never silently shortened
    # beneath a field name that promises a complete recorded value.
    columns.extend(f"substr({_saved(db, key)},1,300) AS {key}_preview" for key in fields)
    columns.extend(f"length({_saved(db, key)}) AS {key}_characters" for key in fields)
    for key in ('question', 'answer', 'rationale'):
        columns += [f"substr({_saved(db, key)},1,300) AS {key}_preview",
                    f"length({_saved(db, key)}) AS {key}_characters"]
    return ','.join(columns)


def _version_summary(row, decision_id):
    out = dict(row)
    out['snapshot_omitted'] = True
    out['state_basis'] = 'immutable recorded state; current authorization is separate'
    out['read'] = {'decision_id': decision_id, 'version_id': out['id']}
    return out


def _authority(current):
    return {key: current[key] for key in ('id', 'status', 'signoff', 'needs_review',
            'authorized', 'approval_pending', 'updated_at') if key in current}


def _saved_revision(current):
    revision = current.get('revision')
    if not isinstance(revision, dict):
        return None
    return {key: revision[key] for key in ('id', 'decision_id', 'created_at') if key in revision} | {
        'identifier_kind': 'decision_revisions',
        'notice': 'Saved signed-revision metadata, not current authorization. This ID is not a decision_versions ID.',
        'read': {'decision_id': current['id'], 'detail': 'full'}}


def _snapshot(db, current):
    # Fingerprints cover immutable version content. Counts/IDs, not timestamp
    # uniqueness, detect newly committed history even with equal timestamps.
    mark = hashlib.sha256(json.dumps(current, sort_keys=True, separators=(',', ':')).encode())
    for row in db.execute('SELECT id,sequence,fingerprint FROM decision_versions WHERE decision_id=? ORDER BY sequence,id',
                          (current['id'],)):
        mark.update(json.dumps(list(row), separators=(',', ':')).encode())
    return mark.hexdigest()


def _compact_current(current, omitted):
    out = dict(current)
    if isinstance(out.get('revision'), dict):
        revision = out.pop('revision')
        omitted['revision'] = {'json_characters': len(json.dumps(revision)),
                               'read': {'decision_id': current['id'], 'detail': 'full'}}
        out['saved_revision'] = _saved_revision(current)
    return out


def _fit_response(response, out, omitted, pages, decision_id):
    """Fit the actual encoded response, including omissions and seeded history.

    Complete fields are either retained or explicitly omitted. Only labeled
    previews can shrink, and their full/exact reads and original lengths stay.
    """
    protected = {'id', 'run_id', 'status', 'signoff', 'needs_review', 'authorized', 'approval_pending',
                 'updated_at', 'saved_revision', 'fact_revision', 'superseded_by', 'source_id'}
    while _size(response()) > MAX_CONTENT_BYTES:
        best, action = 0, None
        for key, value in out.items():
            if key in protected or key.endswith('_preview'):
                continue
            metadata = {'json_characters': len(json.dumps(value)),
                        'read': {'decision_id': decision_id, 'detail': 'full'}}
            replacement = {}
            if isinstance(value, str):
                metadata['characters'] = len(value)
                replacement[key + '_preview'] = value[:200]
            elif isinstance(value, list):
                metadata['total'] = len(value)
            saving = _size({key: value}) - _size(replacement) - _size({key: metadata})
            if saving > best:
                best, action = saving, ('omit', key, metadata, replacement)
        for container in [out, *(row for rows in pages.values() for row in rows)]:
            for key, value in container.items():
                if not key.endswith('_preview') or not isinstance(value, str) or not value:
                    continue
                shorter = value[:len(value) // 2]
                saving = _size(value) - _size(shorter)
                if saving > best:
                    best, action = saving, ('preview', container, key, shorter)
        if action is None:
            return False
        if action[0] == 'omit':
            _, key, metadata, replacement = action
            del out[key]
            omitted[key] = metadata
            out.update(replacement)
        else:
            _, container, key, shorter = action
            container[key] = shorter
    return True


def get_decision(store, args):
    decision_id = args['decision_id']
    detail, version_id = args.get('detail', 'summary'), args.get('version_id', '')
    if detail not in ('summary', 'full'):
        raise Invalid('detail must be summary or full')
    if not isinstance(version_id, str) or len(version_id) > 100 or ('version_id' in args and not version_id):
        raise Invalid('version_id must be a nonempty exact saved version ID')
    if version_id and any(key in args for key in ('cursor', 'limit')):
        raise Invalid('version_id cannot be combined with cursor or limit')
    if detail == 'full' and any(key in args for key in ('cursor', 'limit')):
        raise Invalid('detail=full cannot be combined with cursor or limit')
    if detail == 'full' and not version_id:
        return store.get_decision(decision_id)
    limit = _limit(args)
    binding = _digest({'decision_id': decision_id, 'limit': limit})
    cursor = _read_cursor(args['cursor'], binding, HISTORY_COLLECTIONS) if 'cursor' in args else None
    with store.connect() as db:
        _begin(db)
        current = store.get_decision(decision_id, _include_history=False, _db=db)
        if version_id:
            row = db.execute('SELECT id,sequence,snapshot,recorded_at FROM decision_versions WHERE decision_id=? AND id=?',
                             (decision_id, version_id)).fetchone()
            if row is None:
                raise Invalid('Decision history version not found on this decision; version_id must be a decision_versions ID, not a signed/proof revision')
            return {'decision_id': decision_id, 'version_id': row['id'], 'sequence': row['sequence'],
                    'recorded_at': row['recorded_at'], 'historical_snapshot': json.loads(row['snapshot']),
                    'current_authorization': _authority(current),
                    'saved_revision': _saved_revision(current),
                    'notice': 'Complete immutable saved snapshot. Its recorded status/signoff/grant is historical; '
                              'current_authorization describes the decision now and does not authorize a new request.'}
        omitted = {}
        for key, sql, params in (
                ('events', 'SELECT count(*) FROM events WHERE decision_id=?', (decision_id,)),
                ('work_item_history', "SELECT count(*) FROM events WHERE run_id=? AND kind='work_item_linked'", (current['run_id'],))):
            omitted[key] = {'total': db.execute(sql, params).fetchone()[0],
                            'read': {'decision_id': decision_id, 'detail': 'full'}}
        snapshot = _snapshot(db, {**current, 'omitted_history_counts': omitted})
        if cursor and cursor['snapshot'] != snapshot:
            raise Invalid('Decision discovery results changed; discard accumulated pages and restart without cursor')
        totals, rows = {}, {}
        starts = cursor['offsets'] if cursor else dict.fromkeys(HISTORY_COLLECTIONS, 0)
        for key in HISTORY_COLLECTIONS:
            where = 'v.decision_id=?' + (' AND ' + _approval_predicate(db) if key == 'historical_approvals' else '')
            totals[key] = db.execute('SELECT count(*) FROM decision_versions v WHERE ' + where, (decision_id,)).fetchone()[0]
            rows[key] = [_version_summary(row, decision_id) for row in db.execute(
                'SELECT ' + _version_columns(db) + ' FROM decision_versions v WHERE ' + where +
                ' ORDER BY v.sequence DESC,v.id LIMIT ? OFFSET ?', (decision_id, limit, starts[key]))]
        if cursor and (any(starts[key] > totals[key] for key in HISTORY_COLLECTIONS)
                       or all(starts[key] == totals[key] for key in HISTORY_COLLECTIONS)):
            raise Invalid('Decision discovery cursor is out of range; restart without cursor')
    out = _compact_current(current, omitted)
    offsets, pages = dict(starts), {key: [] for key in HISTORY_COLLECTIONS}

    def response():
        has_more = any(offsets[key] < totals[key] for key in HISTORY_COLLECTIONS)
        return {**out, **pages, 'projection': {'detail': 'summary', 'notice': NOTICE,
                'omitted_fields': omitted, 'full_read': {'decision_id': decision_id, 'detail': 'full'},
                'history_format': 'metadata and labeled previews; use version_id for complete snapshot'},
                'pagination': {'limit': limit, 'snapshot': snapshot, 'max_content_bytes': MAX_CONTENT_BYTES,
                    'has_more': has_more, 'next_cursor': _cursor(binding, snapshot, offsets) if has_more else None,
                    'collections': {key: {'total': totals[key], 'offset': starts[key], 'returned': len(pages[key]),
                        'has_more': offsets[key] < totals[key], 'truncated': len(pages[key]) < totals[key]}
                        for key in HISTORY_COLLECTIONS},
                    'notice': 'Independent history and recorded-approval pages. Repeat the same decision_id and limit with next_cursor; '
                              'restart if results change. Recorded approvals include saved human approvals and rule uses, not current permission.'}}
    returned = 0
    # Reserve a first entry in each available index before fitting the whole
    # response. Long current text or historical previews cannot hide history.
    for key in reversed(HISTORY_COLLECTIONS):
        if rows[key] and returned < limit:
            pages[key].append(rows[key][0])
            offsets[key] += 1
            returned += 1
    if not _fit_response(response, out, omitted, pages, decision_id):
        raise Invalid('Decision identifiers and history metadata exceed compact budget; use detail=full')
    while returned < limit:
        advanced = False
        # Both indexes advance independently: newer invalidations cannot crowd
        # saved human approval or rule use out of the first page.
        for key in reversed(HISTORY_COLLECTIONS):
            index = offsets[key] - starts[key]
            if index >= len(rows[key]):
                continue
            pages[key].append(rows[key][index])
            offsets[key] += 1
            if _size(response()) > MAX_CONTENT_BYTES:
                pages[key].pop()
                offsets[key] -= 1
                return response()
            returned += 1
            advanced = True
            if returned == limit:
                break
        if not advanced:
            break
    return response()


def search(store, args):
    """Explicit historical evidence search, isolated from automatic reuse."""
    mode = args.get('mode', 'current')
    if mode not in ('current', 'history'):
        raise Invalid('mode must be current or history')
    if mode == 'current':
        if any(key in args for key in ('limit', 'cursor')):
            raise Invalid('limit and cursor are supported only with mode=history')
        return store.search(args['query'], repo=args.get('repo', ''))
    query, repo, limit = args['query'], args.get('repo', ''), _limit(args)
    if not isinstance(query, str) or len(query) > 2000 or not query.strip():
        raise Invalid('query must be nonempty text of at most 2000 characters')
    if not isinstance(repo, str) or len(repo) > 500:
        raise Invalid('repo must be text of at most 500 characters')
    from .graph import _STOP
    all_terms = sorted(set(re.findall(r'[a-z0-9]{3,}', query.lower())) - _STOP)
    terms = all_terms[:12]
    if not terms:
        raise Invalid('Historical search requires a word of at least three letters or digits')
    binding = _digest({'query': query, 'repo': repo, 'limit': limit, 'mode': mode})
    cursor = _read_cursor(args['cursor'], binding, ('matches',)) if 'cursor' in args else None
    # Reuse the existing visibility predicate as an opaque helper. Do not
    # change its internals or the separate memory/candidate eligibility paths.
    visibility, scoped_args = store.graph._memory_scope_sql()
    where, params = 'd.draft=0 AND ' + visibility, list(scoped_args)
    if repo:
        where += " AND (d.repo=? OR d.repo='')"
        params.append(repo)
    with store.connect() as db:
        _begin(db)
        fields = ('question', 'answer', 'rationale', 'context')
        current_text = "lower(" + " || ' ' || ".join(f"coalesce(d.{key},'')" for key in fields) + ')'
        saved_text = "lower(" + " || ' ' || ".join(_saved(db, key) for key in fields) + ')'
        # SQL ranks compact scalar projections. Full snapshots never enter
        # application memory to discover an older matching answer.
        score = lambda expression: ' + '.join(f'CASE WHEN {expression} LIKE ? THEN 1 ELSE 0 END' for _ in terms)
        patterns = ['%' + term + '%' for term in terms]
        cte = ('WITH evidence AS ('
               'SELECT d.id,d.run_id,d.repo,d.status,d.signoff,d.needs_review,d.superseded_by,d.updated_at,'
               "'' AS version_id,0 AS sequence,d.question AS matched_question,d.answer AS matched_answer,"
               + score(current_text) + ' AS score FROM decisions d WHERE ' + where +
               ' UNION ALL SELECT d.id,d.run_id,d.repo,d.status,d.signoff,d.needs_review,d.superseded_by,d.updated_at,'
               'v.id AS version_id,v.sequence,' + _saved(db, 'question') + ' AS matched_question,' +
               _saved(db, 'answer') + ' AS matched_answer,' + score(saved_text) +
               ' AS score FROM decisions d JOIN decision_versions v ON v.decision_id=d.id WHERE ' + where +
               '), ranked AS (SELECT *,row_number() OVER (PARTITION BY id ORDER BY score DESC,sequence DESC,version_id) AS rank '
               'FROM evidence WHERE score>0) ')
        sql_args = [*patterns, *params, *patterns, *params]
        total = db.execute(cte + 'SELECT count(*) FROM ranked WHERE rank=1', sql_args).fetchone()[0]
        # Current values and every immutable version identity bind the cursor;
        # a late equal-timestamp insert cannot pass as the same result set.
        mark = hashlib.sha256()
        for row in db.execute('SELECT d.id,d.updated_at,d.status,d.signoff,d.needs_review,d.superseded_by,'
                              'd.question,d.answer,d.context,d.rationale FROM decisions d WHERE ' + where + ' ORDER BY d.id', params):
            mark.update(json.dumps(list(row), separators=(',', ':')).encode())
        for row in db.execute('SELECT v.id,v.fingerprint FROM decision_versions v JOIN decisions d ON d.id=v.decision_id WHERE '
                              + where + ' ORDER BY v.id', params):
            mark.update(json.dumps(list(row), separators=(',', ':')).encode())
        snapshot = mark.hexdigest()
        if cursor and cursor['snapshot'] != snapshot:
            raise Invalid('Historical search results changed; discard accumulated pages and restart without cursor')
        offset = cursor['offsets']['matches'] if cursor else 0
        if cursor and offset >= total:
            raise Invalid('Historical search cursor is out of range; restart without cursor')
        rows = db.execute(cte + 'SELECT id,run_id,repo,status,signoff,needs_review,superseded_by,updated_at,version_id,score,'
                          'substr(matched_question,1,300) AS question_preview,length(matched_question) AS question_characters,'
                          'substr(matched_answer,1,300) AS answer_preview,length(matched_answer) AS answer_characters '
                          'FROM ranked WHERE rank=1 ORDER BY score DESC,updated_at DESC,id LIMIT ? OFFSET ?',
                          [*sql_args, limit, offset]).fetchall()
    matches = []

    def response():
        end = offset + len(matches)
        return {'matches': matches, 'mode': 'history', 'query_terms': terms,
                'query_terms_omitted': max(0, len(all_terms) - len(terms)),
                'search_basis': 'case-insensitive literal term overlap in current and saved question, answer, rationale and context',
                'notice': 'Historical evidence search includes needs-review, withdrawn and superseded decisions. '
                          'Matches and previews never authorize reuse. Current status is separate from the matched saved version. '
                          'Read each decision and relevant exact versions; no matches do not establish absence of prior decisions.',
                'pagination': {'total': total, 'offset': offset, 'returned': len(matches), 'limit': limit,
                    'has_more': end < total, 'max_content_bytes': MAX_CONTENT_BYTES,
                    'next_cursor': _cursor(binding, snapshot, {'matches': end}) if end < total else None}}
    for row in rows:
        item = dict(row)
        item['current_state'] = {key: item.pop(key) for key in ('status', 'signoff', 'needs_review', 'superseded_by', 'updated_at')}
        item['matched_version_id'] = item.pop('version_id') or None
        item['read'] = {'decision_id': item['id']}
        item['exact_read'] = ({'decision_id': item['id'], 'version_id': item['matched_version_id']}
                              if item['matched_version_id'] else {'decision_id': item['id'], 'detail': 'full'})
        item['request_applicability'] = 'not_evaluated_by_this_read'
        matches.append(item)
        if _size(response()) > MAX_CONTENT_BYTES:
            matches.pop()
            if not matches:
                raise Invalid('One search summary exceeds compact budget; narrow the historical query')
            break
    return response()
