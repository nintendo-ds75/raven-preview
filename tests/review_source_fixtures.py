"""Adapt older synthetic code/evidence fixtures to source-owned review inputs.

The fixtures in these suites test code grounding, witness parsing and storage,
not extraction. Their old display labels are discarded explicitly; the new
source-contract suite exercises raw missing/weakened labels without this adapter.
Captured live reports are never rewritten by this helper.
"""
from copy import deepcopy
from unittest.mock import patch
from bridge import llm


def _legacy_clear(raw):
    checks = raw.get('checks')
    if raw.get('status') != 'complete' or not isinstance(checks, list) or len(checks) > 1 or raw.get('exits', []) != []:
        return False
    for row in checks:
        if (not isinstance(row, dict) or type(row.get('n')) is not int or row['n'] != 1
                or set(row) - {'n', 'at', 'assessment', 'counterexample', 'not_shown', 'allegation'}
                or not any(key in row for key in ('assessment', 'counterexample', 'not_shown'))
                or row.get('assessment') not in (None, 'honored') or row.get('allegation') is not None):
            return False
        if any(not isinstance(row.get(key, ''), str) or row.get(key, '').strip() for key in ('counterexample', 'not_shown')):
            return False
        if not isinstance(row.get('at', ''), str) or len(row.get('at', '')) > 300:
            return False
    return True


def source_conformance(cfg, question, answer, diff, *args, preserve_needs=False, **kwargs):
    original = llm.Client.complete_json
    source = llm._approved_sources(answer)[0]

    def complete(client, purpose, *values, **options):
        raw = deepcopy(original(client, purpose, *values, **options))
        if not isinstance(raw, dict):
            return raw
        if purpose == 'conformance' and isinstance(raw.get('requirements'), list):
            raw['schema'] = 'source-checks-v1'
            raw['source_coverage'] = [{'source_id': source['id'], 'scope': 'all_obligations'}]
            for row in raw['requirements']:
                if llm._requirement_shape(row):
                    row['source_id'] = source['id']
                    if not preserve_needs:
                        row.pop('needs', None)
        elif purpose == 'counterexample' and _legacy_clear(raw):
            at = raw['checks'][0].get('at', '') if raw['checks'] else ''
            raw = {**{key: value for key, value in raw.items() if key not in ('checks', 'exits')},
                   'schema': 'coverage-v2', 'status': 'complete',
                   'checks': [{'n': 1, 'source_id': source['id'], 'scope': 'all_obligations', 'assessment': 'honored', 'at': at}],
                   'findings': [], 'exits_checked': list(range(1, len(llm.early_exits(diff[:llm.DIFF_READ])) + 1)),
                   **({'unexamined': raw['unexamined']} if 'unexamined' in raw else {})}
        return raw

    with patch.object(llm.Client, 'complete_json', new=complete):
        return llm.check_conformance(cfg, question, answer, diff, *args, **kwargs)
