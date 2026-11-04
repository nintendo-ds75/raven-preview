"""Readable text for an objective-scope decision.

Review & take point on the Today page creates an ordinary decision whose
context is the proposed scope as JSON (objectives.governance_context): the
outcome, success and stop criteria, guardrails, the positive, negative,
boundary and non-implication cases, the citations and two digests. That
JSON is what the signature binds to and it stays stored as it is. A person
reads it as a few sentences and short lists instead: measured with
customers, raw JSON in the inbox card, the task page and Slack was the
first complaint.

Everything here is a pure function of the stored text. Nothing is
invented: a field that is missing is left out, a malformed context gives
None or empty values, never an exception.
"""
import json

CATEGORY = 'objective_scope'
REF_PREFIX = 'objective-acceptance:'
OPTIONS = ('Yes, take point', 'Not now', 'Change the scope')
# The stored case keys (objective_head validates the first four) and the
# labels a person reads them under.
CASES = (('positive', 'Will'), ('negative', "Won't"), ('boundary', 'Edge'), ('non_implication', 'Does not imply'))
_ALIASES = {'non_implication': ('non_implication', 'non_implications')}
# canvas.add_node appends these lines to a node's context.
_TRAILERS = ('Paths: ', 'Options: ')
SLACK_LIMIT = 700
_BULLETS = ('- ', '* ', '• ', '– ', '— ')


def is_objective(category='', client_ref=''):
    """Whether a decision is the acceptance of an objective proposal."""
    return str(category or '') == CATEGORY or str(client_ref or '').startswith(REF_PREFIX)


def parse(context, category='', client_ref=''):
    """The governance dict an objective decision's context holds, else None.

    The context is objective JSON when the decision is marked as one
    (category or client_ref) and holds a JSON object, or when an
    unmarked JSON object has the proposal's shape (cases and an outcome,
    guardrails or title). The proposal id in the client_ref is recorded
    as `proposal_id` so a card can link back to Today."""
    if not isinstance(context, str):
        return None
    lines = context.split('\n')
    while lines and lines[-1].startswith(_TRAILERS):
        lines.pop()
    text = '\n'.join(lines).strip()
    if not text.startswith('{'):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    shaped = isinstance(data.get('cases'), dict) and any(isinstance(data.get(k), str) for k in ('outcome', 'guardrails', 'title'))
    if not (is_objective(category, client_ref) or shaped):
        return None
    gov = dict(data)
    ref = str(client_ref or '')
    if ref.startswith(REF_PREFIX) and not _str(gov.get('proposal_id')):
        ident = ref[len(REF_PREFIX):].strip()
        if ident:
            gov['proposal_id'] = ident
    return gov


def _str(value):
    return value.strip() if isinstance(value, str) else ''


def _clip(text, limit):
    """One line, at most `limit` characters, cut at a word and marked."""
    text = ' '.join(str(text or '').split())
    if len(text) <= limit:
        return text
    cut = text[:max(limit - 1, 1)].rsplit(' ', 1)[0].rstrip(',;:.')
    return (cut or text[:max(limit - 1, 1)]) + '…'


def _items(value):
    """A case as a list of short lines: a list of strings as given, a
    string split at its line breaks with bullet marks removed."""
    if isinstance(value, list):
        raw = [v for v in value if isinstance(v, str)]
    elif isinstance(value, str):
        raw = value.split('\n')
    else:
        return []
    out = []
    for line in raw:
        line = ' '.join(line.split())
        for mark in _BULLETS:
            if line.startswith(mark):
                line = line[len(mark):].strip()
                break
        if line:
            out.append(line)
    return out


def _cases(gov):
    cases = gov.get('cases') if isinstance(gov, dict) else None
    cases = cases if isinstance(cases, dict) else {}
    out = {}
    for key, _label in CASES:
        value = next((cases[k] for k in _ALIASES.get(key, (key,)) if k in cases), None)
        out[key] = _items(value)
    return out


def _evidence(gov, limit=1500):
    citations = gov.get('citations') if isinstance(gov, dict) else None
    out = []
    for citation in (citations if isinstance(citations, list) else [])[:12]:
        if not isinstance(citation, dict):
            continue
        quote = _str(citation.get('quote'))
        if not quote:
            continue
        title = _str(citation.get('title')) or _str(citation.get('source_title'))
        out.append({'quote': _clip(quote, limit), 'title': title})
    return out


def _what(gov):
    return _str(gov.get('title')) or _str(gov.get('outcome'))


def _question(gov):
    what = _clip(_what(gov), 160)
    return f'Should Raven take point on {what}?' if what else 'Should Raven take point on this?'


def _bound(gov, guard_limit=240, stop_limit=200):
    guard, stop = _str(gov.get('guardrails')), _str(gov.get('stop_criteria'))
    parts = []
    if guard:
        parts.append('Within: ' + _clip(guard, guard_limit).rstrip('.'))
    if stop:
        parts.append('Stop when: ' + _clip(stop, stop_limit).rstrip('.'))
    return '; '.join(parts) + '.' if parts else ''


def summary(gov, outcome_limit=300):
    """At most three sentences: what Raven proposes to take point on, the
    question being asked, and the bound."""
    if not isinstance(gov, dict):
        return ''
    title, outcome = _clip(_str(gov.get('title')), 160).rstrip('.'), _clip(_str(gov.get('outcome')), outcome_limit).rstrip('.')
    if title and outcome:
        first = f'Raven proposes to take point on {title}, so that {outcome}.'
    elif title or outcome:
        first = f'Raven proposes to take point on {title}.' if title else f'Raven proposes to take point so that {outcome}.'
    else:
        first = 'Raven proposes to take point on an objective it has not described.'
    return ' '.join(s for s in (first, 'The question: should Raven take point on this?', _bound(gov)) if s)


def card(gov):
    """Structured fields for a page: outcome, question, options, the cases
    as lists, guardrails, evidence quotes and a link back to Today."""
    if not isinstance(gov, dict):
        gov = {}
    objective_id = _str(gov.get('objective_id')) or _str(gov.get('proposal_id'))
    return {'title': _str(gov.get('title')), 'outcome': _str(gov.get('outcome')), 'question': _question(gov),
            'options': list(OPTIONS), 'cases': _cases(gov), 'guardrails': _str(gov.get('guardrails')),
            'stop_criteria': _str(gov.get('stop_criteria')), 'success_criteria': _str(gov.get('success_criteria')),
            'why_now': _str(gov.get('why_now')), 'judgment_kind': _str(gov.get('judgment_kind')),
            'evidence': _evidence(gov), 'link_to_today': '#today/' + objective_id if objective_id else ''}


def slack_text(gov):
    """The scope as a chat message reads it: the summary, the cases a line
    each, one evidence quote. At most SLACK_LIMIT characters, no JSON."""
    if not isinstance(gov, dict):
        return ''
    lines = [summary(gov, outcome_limit=200)]
    for key, label in CASES[:3]:
        items = _cases(gov)[key]
        if items:
            lines.append(f'{label}: ' + _clip('; '.join(items), 120))
    for item in _evidence(gov, 140)[:1]:
        lines.append('Evidence: “' + item['quote'] + '”' + (f' ({_clip(item["title"], 60)})' if item['title'] else ''))
    while len('\n'.join(lines)) > SLACK_LIMIT and len(lines) > 1:
        lines.pop()
    text = '\n'.join(lines)
    # A summary alone over the limit is one line; clipping it keeps it one.
    return _clip(text, SLACK_LIMIT) if len(text) > SLACK_LIMIT else text
