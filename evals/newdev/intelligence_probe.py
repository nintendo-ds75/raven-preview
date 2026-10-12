"""Live model probe for investigation, retrieval and conversation.

Run with inference credentials in the environment and a frozen checkout:
  python -m evals.newdev.intelligence_probe --repo /path/to/repo --rev SHA --output /tmp/report.json
This is a component integration probe, not a coding-host or real Slack certification.
"""
import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from bridge import canvas, investigation, semantic_index
from bridge.config import load
from bridge.contact_reasoning import choose
from bridge.ingest import index_repo
from bridge.slack_chat import validated_reading
from bridge.store import Store
from evals.pseudonyms import pseudonymize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True)
    parser.add_argument('--rev', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    cfg = load()
    if not cfg.semantic_retrieval:
        raise SystemExit('Enable inference explicitly for this live probe')
    started = time.monotonic()
    report = {'repo': 'urllib3/urllib3', 'revision': args.rev, 'checks': {},
              'limits': ['No coding host was launched.', 'Slack statements and policy records are synthetic.',
                         'The repository and inference calls are real.']}
    with tempfile.TemporaryDirectory(prefix='raven-intelligence-') as tmp:
        store = Store(Path(tmp) / 'probe.db')
        graph = store.graph
        report['ingest'] = index_repo(graph, args.repo, max_commits=250, repo_name='urllib3/urllib3', rev=args.rev,
                                      paths=['src/urllib3/util', 'docs', 'test/test_retry.py'])
        pseudonymize(graph)
        # Synthetic organizational context, explicitly separate from public history.
        graph.add_person('Mira Example', email='mira@example.invalid', slack_id='UMIRA')
        graph.add_person('Theo Example', email='theo@example.invalid', slack_id='UTHEO')
        graph.set_setting('slack_discovery', '1')
        store.add_record({'repo': 'urllib3/urllib3', 'kind': 'doc', 'ref': 'RUNTIME-1', 'title': 'Server-directed pause policy',
            'body': 'A caller may cap the waiting period suggested by a server. The existing default remains unlimited. '
                    'A zero cap skips the wait but still retries. Raw header parsing must remain unchanged. '
                    'Reject negative, NaN and infinite limits. Explain zero and invalid limits in API documentation.',
            'author': 'Mira Example', 'status': 'Approved', 'resolved': True, 'paths': 'src/urllib3/util/retry.py'})
        store.add_record({'repo': 'urllib3/urllib3', 'kind': 'doc', 'ref': 'DOCS-1', 'title': 'Public API release notes',
            'body': 'New constructor options need a release note and API documentation covering defaults and boundary values.',
            'author': 'Theo Example', 'status': 'Approved', 'resolved': True, 'paths': 'docs,src/urllib3/util/retry.py'})
        task = canvas.start_task(store, cfg.without_models(), {'title': 'Bound Retry-After sleeping',
            'goal': 'Let callers cap Retry-After delays without changing existing behavior.',
            'repo': 'urllib3/urllib3', 'facts': 'release=library-next'})['task_id']
        source = subprocess.check_output(['git', '-C', args.repo, 'show', args.rev + ':src/urllib3/util/retry.py'], text=True)
        observations = ('At revision ' + args.rev + ', src/urllib3/util/retry.py parses Retry-After, sleeps for it, '
                        'and clones Retry settings through new()/increment(). We plan an optional constructor limit. '
                        'We have not decided how zero, invalid values or docs should behave. Relevant code:\n' + source[:12000])
        review = investigation.review(store, {'task_id': task, 'observations': observations,
            'paths': ['src/urllib3/util/retry.py', 'docs/reference/urllib3.util.rst']}, cfg)
        report['discovery'] = review
        report['checks']['discovery_completed'] = review['status'] == 'complete'
        report['checks']['discovery_found_choices'] = bool(review['findings'])
        report['checks']['review_created_no_decisions'] = graph.db.execute('SELECT count(*) n FROM decisions').fetchone()['n'] == 0
        print('Discovery:', review['status'], len(review['findings']), 'findings', flush=True)
        query = 'Can an application bound the pause before attempting the request again?'
        found, diagnostic = semantic_index.sources(graph, query, 'urllib3/urllib3')
        report['retrieval'] = {'diagnostic': diagnostic, 'refs': [r['ref'] for r in found]}
        report['checks']['learned_retrieval_active'] = diagnostic['backend'] == 'local'
        report['checks']['policy_retrieved'] = 'RUNTIME-1' in report['retrieval']['refs']
        contact = choose(graph, cfg, 'urllib3/urllib3', query, context='Need a runtime policy decision on the new delay cap.',
                         paths=['src/urllib3/util/retry.py'], facts={'release': 'library-next'}, task_id=task)
        report['contacts'] = contact
        report['checks']['runtime_contact_found'] = bool(contact and contact[0][0] == 'Mira Example')
        report['checks']['no_authority_invented'] = not graph.authority_rows('urllib3/urllib3')
        print('First contact:', contact[0][0] if contact else 'none', flush=True)
        readings = []
        for message, expected in [('Not me, please ask Theo Example just this one.', 'handoff'),
                                  ('I will look tomorrow.', 'chat'),
                                  ('Zero should skip sleeping while keeping the retry. This is for library-next only.', 'answer')]:
            result = validated_reading(cfg, {'question': 'What should a zero Retry-After cap do?',
                'answer': '', 'on_table': '', 'context': 'Optional limit for Retry-After sleeping.',
                'task': {'title': 'Bound retries', 'facts': 'release=library-next'},
                'history': [], 'pending_readback': None, 'message': message, 'sources': []})
            readings.append({'message': message, 'expected': expected, 'reading': result})
        report['slack_readings'] = readings
        report['checks']['natural_reply_intents'] = all(r['reading']['kind'] == r['expected'] for r in readings)
        report['events'] = [{'kind': r['kind'], 'detail': json.loads(r['detail'])} for r in graph.db.execute(
            "SELECT kind,detail FROM events WHERE run_id=? AND kind IN ('contact_reasoned','discovery_reviewed') ORDER BY id", (task,))]
        graph.close()
    report['seconds'] = round(time.monotonic() - started, 2)
    report['models'] = {'main': cfg.model, 'fast': cfg.fast_model, 'embeddings': os.environ.get('BRIDGE_EMBEDDING_MODEL', semantic_index.MODEL)}
    Path(args.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report['checks'], indent=2), flush=True)
    raise SystemExit(0 if all(report['checks'].values()) else 1)


if __name__ == '__main__':
    main()
