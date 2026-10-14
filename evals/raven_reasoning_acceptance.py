"""Opt-in production Raven reasoning acceptance, controlled through public APIs.

This is not an autonomous-host test. Synthetic people act through authenticated
HTTP; a deterministic controller issues MCP requests. Only provider transport is
replaced in live mode. Expectations stay in this evaluator, never task prompts.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from bridge import canvas, llm
from bridge.auth import Auth
from bridge.server import make_server
from bridge.store import Store
from evals.claude_mcp_acceptance import exception_diagnostic
from evals.live_workflow_acceptance import AcceptanceFailure

MODEL = 'claude-sonnet-4-6'
LIMITS = {'owners': {'max_calls': 16, 'max_input_bytes': 300000, 'max_output_tokens': 16000},
          'cache': {'max_calls': 16, 'max_input_bytes': 300000, 'max_output_tokens': 26000}}
PEOPLE = ('Ari Vale', 'Bela Stone', 'Ciro Lake', 'Dana Reed', 'Eli Moss')
ACCEPTABLE_CONTACTS = frozenset(('Ciro Lake', 'Dana Reed'))
RESPONDENT = 'Eli Moss'
FACTS = {'customer': 'Synthetic Juniper', 'org': 'Synthetic North',
         'environment': 'production', 'domain': 'metering'}
OWNER_QUESTION = 'Should synthetic internal batch requests count toward customer invoiced usage?'
OWNER_HELDOUT = 'Should synthetic internal batch requests count toward customer invoiced usage during retries?'
PRIOR_QUESTION = 'How many full days should production audit events remain, including the exact age boundary?'
CACHE_HELDOUT = 'Which production audit events may the cleanup job discard by age, and what happens at the retention boundary?'
PREMISE = 'Production audit-event age is measured from ingestion time.'


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def urlopen(request, *, timeout):
    """Local synthetic credentials never use a cached ambient proxy or redirect."""
    return build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout)


def require(value, code):
    if not value:
        raise AcceptanceFailure(code)


def usage(gateway):
    return gateway.snapshot() if gateway is not None else {'fixture_only': True}


class ReasoningFixture:
    """Only workspace bootstrap writes directly; scenario actions use HTTP/MCP."""
    def __init__(self, root, case, gateway=None, model_double=None, timeout=180):
        self.root, self.case, self.gateway = Path(root), case, gateway
        self.repo = 'synthetic/reasoning-' + case
        self.path = 'billing/usage.py' if case == 'owners' else 'policy/retention.py'
        self.deadline = time.monotonic() + timeout
        self.transcript, self.model_calls, self.people, self.tokens = [], [], {}, {}
        self.serial = 0
        self.stopping = False
        self.stack = ExitStack()
        self.server = self.store = None
        self.bootstrap = 'synthetic-bootstrap-' + secrets.token_hex(16)
        clean = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG', 'HOME')}
        clean.update(BRIDGE_ADMIN_TOKEN=self.bootstrap, BRIDGE_SECRET=secrets.token_hex(32),
                     BRIDGE_MODEL_API='anthropic', BRIDGE_MODEL=MODEL, BRIDGE_FAST_MODEL=MODEL,
                     BRIDGE_SEMANTIC='0', BRIDGE_DEEP='0', BRIDGE_LIVE='0', BRIDGE_EMBEDDINGS='disabled',
                     ANTHROPIC_API_KEY=gateway.placeholder if gateway else 'offline-model-double')
        self.stack.enter_context(patch.dict(os.environ, clean, clear=True))
        if gateway is not None:
            def bounded_transport(request, *args, **kwargs):
                remaining = self.remaining()
                kwargs['timeout'] = min(float(kwargs.get('timeout', 120)), remaining)
                return gateway.urllib_open(request, *args, **kwargs)
            self.stack.enter_context(patch('bridge.llm._open_model_request', new=bounded_transport))
        else:
            self.stack.enter_context(patch('bridge.llm._open_model_request', side_effect=AssertionError('No live transport')))
        original = llm.Client.complete
        def observe(client, purpose, system, prompt, *args, **kwargs):
            self.remaining()
            entry = {'purpose': purpose, 'stage': self.stage, 'completed': False,
                     'requested_max_tokens': kwargs.get('max_tokens', args[0] if args else 2000)}
            self.model_calls.append(entry)
            try:
                result = (model_double(purpose, system, prompt, *args, **kwargs) if model_double is not None
                          else original(client, purpose, system, prompt, *args, **kwargs))
                entry['completed'] = True
                return result
            except Exception as error:
                entry['error'] = exception_diagnostic(error)
                raise
        self.stack.enter_context(patch.object(llm.Client, 'complete', new=observe))
        self.stage = 'bootstrap'
        try:
            self.root.mkdir(parents=True)
            self.store = Store(self.root / 'raven.db')
            self.store.graph.set_setting('workspace_name', 'Synthetic reasoning acceptance')
            self.auth = Auth(self.store, enabled=True)
            self.server = make_server(self.store, port=0, auth=self.auth, wait_cap=2, github_api=None)
            self.url = f'http://127.0.0.1:{self.server.server_port}'
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
            self.thread.start()
            for name in ('Quinn Operator', *PEOPLE):
                row = self.http('/api/people', {'name': name, 'role': 'admin' if name == 'Quinn Operator' else 'member',
                    'email': name.lower().replace(' ', '.') + '@synthetic.invalid'}, token=self.bootstrap)
                self.people[name] = row['id']
                self.tokens[name] = self.http('/api/tokens', {'person_id': row['id'], 'kind': 'human'},
                                              token=self.bootstrap)['token']
            self.agent = self.http('/api/tokens', {'person_id': self.people['Quinn Operator'], 'kind': 'agent'},
                                   token=self.bootstrap)['token']
            self.mcp('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                    'clientInfo': {'name': 'reasoning-evaluator', 'version': '1'}})
        except BaseException:
            self.close()
            raise

    def http(self, path, data=None, *, person=None, token=None, expected=200):
        remaining = self.remaining()
        token = token or self.tokens[person or 'Quinn Operator']
        req = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                               'Accept': 'application/json, text/event-stream'})
        try:
            response = urlopen(req, timeout=min(120, remaining))
        except HTTPError as error:
            response = error
        with response:
            status, value = response.status, json.loads(response.read())
        if path != '/api/tokens':
            self.transcript.append({'stage': self.stage, 'path': path, 'status': status})
        require(status == expected, 'http_status_' + str(status))
        return value

    def remaining(self):
        require(not self.stopping, 'fixture_stopping')
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'reasoning_timeout')
        return remaining

    def mcp(self, method, params, *, operator=False):
        self.serial += 1
        result = self.http('/mcp', {'jsonrpc': '2.0', 'id': self.serial, 'method': method, 'params': params},
                           token=self.tokens['Quinn Operator'] if operator else self.agent)
        require('error' not in result, 'mcp_protocol_error')
        return result['result']

    def tool(self, name, *, operator=False, **args):
        envelope = self.mcp('tools/call', {'name': name, 'arguments': args}, operator=operator)
        self.transcript.append({'stage': self.stage, 'tool': name, 'arguments': args,
                                'is_error': bool(envelope.get('isError'))})
        require(not envelope.get('isError'), 'mcp_' + name + '_refused')
        return json.loads(envelope['content'][0]['text'])

    def full(self, node):
        return self.tool('bridge_get_decision', decision_id=node, detail='full')

    def wait_ready(self, task, node=None):
        while time.monotonic() < self.deadline:
            require(not usage(self.gateway).get('halted'), 'provider_halted')
            tree = self.tool('bridge_get_tree', task_id=task)
            if not tree.get('model_pending'):
                if node is None:
                    return tree
                result = self.full(node)
                if not result.get('model_pending'):
                    return result
            time.sleep(.05)
        raise AcceptanceFailure('reasoning_timeout')

    @contextmanager
    def inference(self, stage):
        self.stage = stage
        with patch.dict(os.environ, {'BRIDGE_SEMANTIC': '1'}):
            yield
            require(canvas.wait_for_background(self.remaining()), 'background_not_quiescent')

    def import_record(self, ref, body, author, *, kind='doc', resolved=False, **extra):
        return self.tool('bridge_import_record', repo=self.repo, kind=kind, ref=ref, title=extra.pop('title', ref),
                         body=body, author=author, status='Approved' if resolved else 'Open', resolved=resolved,
                         paths=[self.path], **extra)

    def task(self, question, key, *, facts=None, work_item=False):
        current_facts = dict(FACTS if facts is None else facts)
        if work_item:
            current_facts['work_item'] = key
        task = self.tool('bridge_start_task', title=question, goal=question, repo=self.repo,
                         paths=self.path, facts=json.dumps(current_facts), client_key=key)['task_id']
        if work_item:
            source = self.import_record(key, question + ' No policy answer has been recorded for this request.',
                '', kind='jira', provider='jira', namespace='synthetic-north', external_id=key)['source']
            self.tool('bridge_get_record', repo=self.repo, record_id=source['record_id'])
            self.tool('bridge_link_work_item', task_id=task, repo=self.repo,
                record_id=source['record_id'], source_version_id=source['source_version_id'], provider=source['provider'],
                namespace=source['namespace'], object_kind=source['kind'], external_id=source['external_id'], role='work_item')
        self.wait_ready(task)
        return task

    def node(self, task, question):
        row = self.tool('bridge_add_node', task_id=task, question=question, paths=self.path,
                        category='policy', client_ref='policy-question')
        return self.wait_ready(task, row['node_id'])

    def human(self, node, action, data, person):
        return self.http('/api/decisions/' + node + '/' + action, data, person=person)

    def authority(self):
        return self.http('/api/people')['authority']

    def close(self):
        self.stopping = True
        quiescent = True
        if self.store is not None:
            # Cleanup may wait for existing I/O/retry backoff, but cannot launch
            # another inference. Never restore ambient credentials/transport
            # beneath a still-running model worker.
            quiescent = canvas.wait_for_background(10)
        if not quiescent:
            return False  # Keep the fail-closed guards until this CLI exits.
        if self.server is not None:
            self.server.stop_waits()
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None
        if self.store is not None:
            self.store.graph.close()
            self.store = None
        self.stack.close()
        return quiescent


def build_owner_history(fixture):
    checkout = fixture.root / 'checkout'
    (checkout / 'billing').mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'LANG')}
    env.update(GIT_CONFIG_GLOBAL='/dev/null', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_NOSYSTEM='1')
    def git(*args, extra=None):
        return subprocess.run(['git', '-C', str(checkout), *args], env={**env, **(extra or {})},
                              capture_output=True, check=True, timeout=20).stdout.decode().strip()
    git('init', '-q')
    now = datetime.now(timezone.utc)
    for index in range(8):
        (checkout / fixture.path).write_text('\n'.join(f'meter_field_{n} = {index + n}' for n in range(40)) + '\n')
        stamp = (now - timedelta(days=8-index)).isoformat()
        author = PEOPLE[0] if index < 6 else PEOPLE[index-4]
        git('add', '.')
        git('commit', '-qm', 'Regenerate meter field definitions' if index < 6 else 'Review invoice classification compatibility',
            extra={'GIT_AUTHOR_NAME': author, 'GIT_AUTHOR_EMAIL': author.lower().replace(' ', '.') + '@synthetic.invalid',
                   'GIT_COMMITTER_NAME': PEOPLE[1], 'GIT_COMMITTER_EMAIL': 'bela.stone@synthetic.invalid',
                   'GIT_AUTHOR_DATE': stamp, 'GIT_COMMITTER_DATE': stamp})
    # Leave the majority of present lines with the mechanical author as well.
    (checkout / fixture.path).write_text('\n'.join(f'meter_field_{n} = {90+n}' for n in range(40)) + '\n')
    git('add', '.')
    git('commit', '-qm', 'Regenerate meter field definitions', extra={
        'GIT_AUTHOR_NAME': PEOPLE[0], 'GIT_AUTHOR_EMAIL': 'ari.vale@synthetic.invalid',
        'GIT_COMMITTER_NAME': PEOPLE[1], 'GIT_COMMITTER_EMAIL': 'bela.stone@synthetic.invalid'})
    # The supported operator CLI performs installation ingestion. An authenticated
    # agent token intentionally cannot read arbitrary server-side checkout paths.
    subprocess.run([sys.executable, '-m', 'bridge', 'ingest', str(checkout), '--db', str(fixture.root / 'raven.db'),
                    '--repo', fixture.repo], env={**os.environ, 'BRIDGE_MODEL_API': 'none', 'BRIDGE_SEMANTIC': '0'},
                   capture_output=True, check=True, timeout=30)
    for ref, author, title, body in (
        ('INVESTIGATION-1', PEOPLE[2], 'Internal batch invoiced usage compatibility review',
         'Reviewed the customer invoice impact of internal batch traffic classification. The new inclusion decision remains open.'),
        ('INVESTIGATION-2', PEOPLE[3], 'Internal batch invoiced usage product review',
         'Investigated customer invoice expectations and internal batch retry classification. The new policy choice remains open.'),
        ('INCIDENT-1', PEOPLE[1], 'Internal batch usage collector on-call incident',
         'Current collector on-call. Restored ingestion after a queue spike and merged the operational fix. Customer invoice classification was not changed.'),
        ('GENERATION-1', PEOPLE[0], 'Internal batch usage generated meter fields',
         'Regenerated meter field definitions and bulk formatting. The change retained existing customer invoice behavior.')):
        fixture.import_record(ref, body, author, title=title)
    return {'head': git('rev-parse', 'HEAD'), 'blame': git('blame', '--line-porcelain', fixture.path).count('author Ari Vale'),
            'has_codeowners': (checkout / 'CODEOWNERS').exists()}


def contact_receipt(row, acceptable_ids):
    events = [e for e in row.get('events', []) if e.get('kind') == 'contact_reasoned']
    valid = []
    for event in events:
        detail = event.get('detail', {})
        detail = json.loads(detail) if isinstance(detail, str) else detail
        selected = next((c for c in detail.get('candidates', []) if c.get('id') == detail.get('selected')), None)
        if selected and selected.get('person_id') in acceptable_ids and detail.get('evidence') and all(
                cite in selected.get('evidence', {}) for cite in detail['evidence']):
            valid.append({'selected_person_id': selected['person_id'], 'selected_name': selected['name'],
                          'cited_evidence': {cite: selected['evidence'][cite] for cite in detail['evidence']},
                          'available_evidence': selected['evidence'],
                          'candidate_count': len(detail['candidates'])})
    require(bool(valid), 'missing_acceptable_model_contact_receipt')
    receipt = valid[-1]
    require(row.get('owner_name') == receipt['selected_name'], 'final_owner_differs_from_model_receipt')
    require(not row['authorized'] and not json.loads(row.get('signatures') or '[]'), 'contact_inference_created_authorization')
    return receipt


def contact_diagnostics(row):
    """Retain production validation categories, never free-form model content."""
    categories = {'selected', 'explicit_abstention', 'invalid_result_type', 'missing_candidate',
                  'invalid_candidate_type', 'unknown_candidate', 'missing_evidence', 'invalid_evidence_type',
                  'empty_evidence', 'invalid_evidence_id_type', 'unknown_evidence', 'cross_candidate_evidence',
                  'abstention_with_evidence', 'model_error', 'invalid_json', 'source_access_changed'}
    result = []
    for event in row.get('events', []):
        if event.get('kind') != 'contact_reasoned':
            continue
        detail = event.get('detail', {})
        detail = json.loads(detail) if isinstance(detail, str) else detail
        if not isinstance(detail, dict):
            continue
        candidates = detail.get('candidates', [])
        candidate_ids = [c['id'] for c in candidates if isinstance(c, dict)
                         and isinstance(c.get('id'), str) and re.fullmatch(r'c[1-9][0-9]?', c['id'])]
        selected, cited = detail.get('selected'), detail.get('evidence')
        result.append({'validation': detail['validation'] if detail.get('validation') in categories else 'unavailable',
                       'candidate_count': len(candidates), 'candidate_ids': candidate_ids,
                       'selected': selected if isinstance(selected, str) and selected in candidate_ids else '',
                       'cited_evidence_count': len(cited) if isinstance(cited, list) else 0,
                       'why_present': bool(detail.get('why')),
                       'attempt': detail.get('attempt') if type(detail.get('attempt')) is int else None,
                       'selection_attempt': detail.get('selection_attempt') if type(detail.get('selection_attempt')) is int else None,
                       'repair': detail.get('repair') is True})
    return result


def retention_receipt(answer, duration):
    """Conservative independent text oracle, not a general language classifier.

    Require recognizable positive statements for all three policy constraints.
    Unknown phrasing and recognizable contradictory clauses fail closed.
    """
    text = re.sub(r'\s+', ' ', str(answer).lower().replace('≥', '>=').replace('**', '').replace('`', ''))
    threshold = rf'\b{duration}\s+(?:full\s+)?days?\b'
    clauses = re.split(r'[.;\n]|\b(?:and|but)\b', text)
    delete = r'\b(?:delete|discard|remove|purge|deleted|discarded|removed|purged)\b'
    retain = r'\b(?:retain|keep|retained|kept)\b'
    negative = r'\b(?:no|not|never|zero|don\x27t|mustn\x27t|shouldn\x27t|cannot|can\x27t)\b'
    strict = rf'(?:strictly\s+)?(?:older|more)\s+than\s+{threshold}|>\s*{duration}\s*(?:full\s+)?days?\b'
    boundary = rf'(?:exactly\s+{threshold}|(?:exactly\s+)?at\s+(?:the\s+)?(?:exact\s+)?(?:retention\s+)?boundary)'
    no_delete = rf'\b(?:do\s+not|must\s+not|never|don\x27t)\s+{delete}'
    origin = bool(re.search(r'\b(?:from|since)\s+(?:the\s+)?(?:time\s+of\s+)?ingestion\b|\bingestion\s+time\b', text))
    require(origin and not re.search(r'\b(?:completion|creation|event)\s+time\b|\bnot\s+(?:from\s+)?ingestion\b', text),
            'memory_age_origin_wrong_or_unrecognized')
    inclusive = rf'>=\s*{duration}\b|\bat\s+least\s+{threshold}|{threshold}\s+(?:old\s+)?or\s+(?:older|more)|(?:older|greater)\s+than\s+or\s+equal'
    require(not re.search(inclusive, text), 'memory_inclusive_boundary_contradiction')
    strict_delete = any(re.search(delete, c) and re.search(strict, c) and not re.search(negative, c) for c in clauses)
    retain_boundary = any(re.search(boundary, c) and ((re.search(retain, c) and not re.search(negative, c))
                                                     or re.search(no_delete, c)) for c in clauses)
    negated_retention = any(re.search(boundary, c) and re.search(retain, c) and re.search(negative, c) for c in clauses)
    boundary_deletion = any(re.search(boundary, c) and re.search(delete, c) and not re.search(no_delete, c) for c in clauses)
    wrong_retention = re.search(rf'\b(?:retain|keep)\s+(?:all\s+)?(?:events\s+)?(?:that\s+are\s+)?(?:strictly\s+)?older\s+than\s+{threshold}', text)
    require(strict_delete and retain_boundary and not boundary_deletion and not wrong_retention and not negated_retention,
            'memory_exact_boundary_wrong_or_unrecognized')
    return {'method': 'conservative_independent_text_oracle', 'origin': 'ingestion',
            'delete_age_days': f'> {duration}', 'retain_exact_boundary': True}


def owners_case(f, report):
    acceptable = {f.people[name] for name in ACCEPTABLE_CONTACTS}
    report['oracle'] = {'acceptable_first_contacts': sorted(ACCEPTABLE_CONTACTS), 'respondent': RESPONDENT,
                        'declared_before_inference': True, 'hidden_from_requests': True}
    report['git_fixture'] = build_owner_history(f)
    require(not f.authority(), 'cold_start_authority_not_empty')
    require(f.store.graph.db.execute('SELECT count(*) FROM decisions').fetchone()[0] == 0, 'cold_start_has_decisions')
    report['cold_start'] = {'authority_rows': 0, 'decision_rows': 0}
    with f.inference('cold_owner'):
        task = f.task(OWNER_QUESTION, 'WORK-OWNER-1', work_item=True)
        node = f.node(task, OWNER_QUESTION)
    report['cold_node'] = {k: node.get(k) for k in ('id', 'owner_name', 'owner_evidence', 'authorized', 'signoff', 'model_pending')}
    report['contact_diagnostics'] = {'cold': contact_diagnostics(node)}
    report['cold_contact'] = contact_receipt(node, acceptable)
    require(report['cold_contact']['candidate_count'] >= 3, 'insufficient_plausible_candidates')
    require(not f.authority(), 'contact_reasoning_added_authority')
    report['passed_stages'].append('cold_model_contact_without_approval')
    f.stage = 'explicit_contact_only_referral'
    f.human(node['id'], 'refer', {'person': f.people[RESPONDENT], 'scope_kind': 'contact',
        'note': 'This request needs a policy response from Eli Moss; this is a contact referral only.',
        'expected_updated_at': node['updated_at']}, node['owner_name'])
    current = f.full(node['id'])
    require(not current['authorized'], 'referral_created_authorization')
    f.human(node['id'], 'answer', {'answer': 'Exclude synthetic internal batch requests from invoiced usage; real customer requests remain billable.',
        'applicability': {'requires': FACTS}, 'expected_updated_at': current['updated_at']}, RESPONDENT)
    answered = f.full(node['id'])
    require(answered['authorized'], 'explicit_respondent_answer_missing')
    observations = [json.loads(e['detail']) if isinstance(e['detail'], str) else e['detail']
                    for e in answered.get('events', []) if e.get('kind') == 'contact_observed']
    require(any(e.get('outcome') == 'referred' and e.get('to_person_id') == f.people[RESPONDENT] for e in observations)
            and any(e.get('outcome') == 'answered' and e.get('to_person_id') == f.people[RESPONDENT] for e in observations),
            'referral_response_learning_trail_missing')
    report['contact_outcomes'] = [{k: e.get(k) for k in ('outcome', 'from_person_id', 'to_person_id')} for e in observations]
    require(not any(a['role'] in ('decides', 'approves') for a in f.authority()), 'contact_only_referral_created_broad_authority')
    report['passed_stages'].append('authenticated_contact_referral_and_answer')
    with f.inference('heldout_learned_contact'):
        heldout = f.task(OWNER_HELDOUT, 'WORK-OWNER-2', work_item=True)
        later = f.node(heldout, OWNER_HELDOUT)
    report['heldout_node'] = {k: later.get(k) for k in ('id', 'owner_name', 'owner_evidence', 'authorized', 'signoff', 'model_pending')}
    report['contact_diagnostics']['heldout'] = contact_diagnostics(later)
    report['learned_contact'] = contact_receipt(later, {f.people[RESPONDENT]})
    require(any('learned first contact' in text for text in report['learned_contact']['cited_evidence'].values()),
            'selected_respondent_did_not_cite_scoped_learned_evidence')
    require(task != heldout and node['id'] != later['id'], 'heldout_identity_not_distinct')
    require(not any(a['role'] in ('decides', 'approves') for a in f.authority()), 'learned_contact_created_broad_authority')
    report.update(task_id=task, node_id=node['id'], heldout_task_id=heldout, heldout_node_id=later['id'])
    report['passed_stages'].append('heldout_referral_learning_without_approval_inheritance')


def cache_case(f, report):
    duration = secrets.randbelow(37) + 23
    answer = f'Retain production audit events for {duration} full days from ingestion. Delete only events strictly older than {duration} days; retain events exactly at the boundary.'
    f.stage = 'historical_human_fixture'
    source = f.import_record('AGE-PREMISE', PREMISE, RESPONDENT, resolved=True, title='Production audit event retention age')['source']
    prior_task = f.task(PRIOR_QUESTION, 'CACHE-PRIOR', facts={**FACTS, 'domain': 'audit'})
    prior = f.node(prior_task, PRIOR_QUESTION)
    require(prior.get('owner_name') == RESPONDENT, 'historical_fixture_owner_missing')
    pin = {k: source[k] for k in ('record_id', 'source_version_id')} | {'role': 'support'}
    f.human(prior['id'], 'answer', {'answer': answer, 'expected_updated_at': prior['updated_at'],
        'source_evidence': [pin], 'applicability': {'requires': {'environment': 'production', 'customer': FACTS['customer']}}}, RESPONDENT)
    prior = f.full(prior['id'])
    require(prior['authorized'] and not prior['reusable'], 'historical_personal_signature_missing')
    report['oracle'] = {'duration_days': duration, 'boundary': 'strictly older; exact boundary retained',
                        'heldout_request_contains_answer': False}
    with f.inference('live_memory_reuse'):
        task = f.task(CACHE_HELDOUT, 'CACHE-HELDOUT', facts={**FACTS, 'domain': 'audit'})
        node = f.node(task, CACHE_HELDOUT)
    report['reuse'] = {k: node.get(k) for k in ('id', 'source', 'source_id', 'source_revision', 'answer', 'signoff', 'authorized')}
    purposes = {c['purpose'] for c in f.model_calls if c['stage'] == 'live_memory_reuse' and c['completed']}
    require({'compose', 'support'} <= purposes, 'live_memory_composition_not_exercised')
    require(node.get('source') == 'memory' and node.get('source_id') == prior['id'], 'heldout_did_not_reuse_prior')
    require(node.get('source_revision') == prior['updated_at'], 'memory_source_revision_missing_or_changed')
    report['reuse']['policy_check'] = retention_receipt(node['answer'], duration)
    require(not node['authorized'] and node['signoff'] == 'required', 'memory_authority_wrong')
    require(task != prior_task, 'heldout_task_not_distinct')
    report['passed_stages'].append('live_nonverbatim_memory_reuse_requires_signature')
    f.stage = 'source_freshness_lifecycle'
    f.human(node['id'], 'signoff', {'expected_updated_at': node['updated_at']}, node['owner_name'])
    require(f.full(node['id'])['authorized'], 'derived_human_signoff_missing')
    f.http('/api/settings', {'auto_rules': True})
    signed = f.full(node['id'])
    f.human(node['id'], 'rule', {'scope': 'same', 'conditions': 'environment=production',
                               'expected_updated_at': signed['updated_at']}, signed['owner_name'])
    with f.inference('warm_explicit_grant'):
        warm_task = f.task(CACHE_HELDOUT, 'CACHE-WARM', facts={**FACTS, 'domain': 'audit'})
        before_usage, before_calls = usage(f.gateway), len(f.model_calls)
        warm = f.node(warm_task, CACHE_HELDOUT)
        after_usage, after_calls = usage(f.gateway), len(f.model_calls)
        repeated = f.node(warm_task, CACHE_HELDOUT)
        require(repeated['id'] == warm['id'] and usage(f.gateway) == after_usage and len(f.model_calls) == after_calls,
                'unchanged_node_retry_not_inference_free')
    require(warm['authorized'] and warm['signoff'] == 'rule' and warm['source_id'] == node['id'],
            'explicit_warm_grant_not_applied')
    report['warm_cache'] = {'task_id': warm_task, 'node_id': warm['id'], 'source_id': warm['source_id'],
                            'signoff': 'rule', 'authorized': True, 'new_node_model_operations': after_calls - before_calls,
                            'new_node_physical_requests': after_usage.get('physical_requests', 0) - before_usage.get('physical_requests', 0),
                            'unchanged_node_retry_additional_inference': 0,
                            'policy_check': retention_receipt(warm['answer'], duration)}
    report['passed_stages'].append('explicit_grant_reuse_and_inference_free_unchanged_retry')
    same = f.import_record('AGE-PREMISE', PREMISE, RESPONDENT, resolved=True, title='Production audit event retention age')
    require(not same['changed'] and same['source']['source_version_id'] == source['source_version_id'], 'identical_import_changed_version')
    require(all(f.full(i)['authorized'] for i in (prior['id'], node['id'], warm['id'])), 'identical_import_invalidated_signature')
    changed = f.import_record('AGE-PREMISE', 'Production audit-event age is measured from completion time.',
                              RESPONDENT, resolved=True, title='Production audit event retention age')['source']
    for identifier in (prior['id'], node['id'], warm['id']):
        row = f.full(identifier)
        require(not row['authorized'] and row['needs_review'], 'changed_source_failed_to_invalidate')
    reverted = f.import_record('AGE-PREMISE', PREMISE, RESPONDENT, resolved=True, title='Production audit event retention age')['source']
    require(len({source['source_version_id'], changed['source_version_id'], reverted['source_version_id']}) == 3,
            'source_reversion_reused_version')
    require(source['fingerprint'] == reverted['fingerprint'], 'source_reversion_not_same_content')
    for identifier in (prior['id'], node['id'], warm['id']):
        row = f.full(identifier)
        require(not row['authorized'] and row['needs_review'], 'source_reversion_revived_signature')
    report.update(task_id=task, node_id=node['id'], prior_task_id=prior_task, prior_node_id=prior['id'],
                  source_versions=[source['source_version_id'], changed['source_version_id'], reverted['source_version_id']])
    report['passed_stages'].append('identical_import_and_source_A_B_A_nonrevival')


def run(*, case, output_dir=None, gateway=None, model_double=None, timeout=180):
    if case not in LIMITS or (gateway is None and model_double is None):
        return {'status': 'blocked', 'reason': 'Select a case and explicit live gateway or offline model double.'}
    require(not (gateway is not None and model_double is not None), 'ambiguous_model_boundary')
    root = Path(output_dir)
    if root.exists() and any(root.iterdir()):
        return {'status': 'blocked', 'reason': 'Use a new empty output directory.'}
    root.mkdir(parents=True, exist_ok=True)
    report = {'status': 'failed', 'case': case, 'live_provider': gateway is not None, 'passed_stages': [],
              'controller': 'deterministic public MCP/HTTP; no autonomous coding host', 'synthetic_humans': True,
              'slack': 'disconnected', 'model': MODEL, 'semantic': True, 'deep': False, 'embeddings': 'disabled',
              'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    fixture = None
    try:
        fixture = ReasoningFixture(root / 'fixture', case, gateway, model_double, timeout)
        (owners_case if case == 'owners' else cache_case)(fixture, report)
        require(not gateway or usage(gateway).get('physical_requests', 0) > 0, 'no_provider_requests')
        report['status'] = 'passed' if gateway is not None else 'fixture_passed'
    except Exception as error:
        report.update(failure_code=error.code if isinstance(error, AcceptanceFailure) else 'runner_error',
                      diagnostic=exception_diagnostic(error))
    finally:
        if fixture is not None:
            quiescent = fixture.close()
            report['background_quiescent'] = quiescent
            if not quiescent:
                report.update(status='blocked', failure_code='background_not_quiescent_at_close')
            report['model_calls'] = fixture.model_calls
            (root / 'api-trace.json').write_text(json.dumps(fixture.transcript, indent=2) + '\n')
        report['provider_usage'] = usage(gateway)
        if report['provider_usage'].get('active_requests'):
            report.update(status='blocked', failure_code='provider_not_quiescent_at_close')
        (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--case', choices=sorted(LIMITS), required=True)
    parser.add_argument('--credential-file', type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--workspace-id', default='')
    parser.add_argument('--timeout', type=int, default=240)
    args = parser.parse_args(argv)
    if not args.run_live:
        print(json.dumps({'status': 'blocked', 'reason': 'Explicit --run-live is required.'}))
        return 1
    if not args.credential_file or not args.output_dir:
        parser.error('Live mode requires an installed credential file and a new output directory.')
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        print(json.dumps({'status': 'blocked', 'reason': 'Output directory is not empty.'}))
        return 1
    from evals.customer_provider_gateway import Gateway
    from evals.customer_test_credentials import read_saved_key
    try:
        key = read_saved_key(args.credential_file)
    except Exception:
        print(json.dumps({'status': 'blocked', 'failure_code': 'saved_credential_unavailable'}))
        return 1
    with Gateway(api_key=key, workspace_id=args.workspace_id, model=MODEL, **LIMITS[args.case]) as gateway:
        key = ''
        result = run(case=args.case, output_dir=args.output_dir, gateway=gateway, timeout=args.timeout)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
