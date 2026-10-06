"""Two synthetic, real-provider checks. Requires --run-live and secure env setup.

No Slack service, customer data or approval is involved. At most three physical
Anthropic HTTP requests are allowed across the two logical model operations.
"""
import argparse
import json
import tempfile
import urllib.request
from pathlib import Path
from unittest.mock import patch

from bridge import canvas, interview, llm, slack_chat
from bridge.authz import Actor
from bridge.config import Config, load
from bridge.store import Store


def run():
    cfg = load()
    if cfg.model_api != 'anthropic' or not cfg.api_key or not cfg.semantic_retrieval:
        return {'status': 'blocked', 'reason': 'Configure Anthropic securely on the server and enable inference first.'}
    before = (llm.USAGE.calls, llm.USAGE.input_tokens, llm.USAGE.output_tokens)
    calls = []
    actual_open = urllib.request.urlopen

    def budgeted_open(request, *args, **kwargs):
        url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
        if url != 'https://api.anthropic.com/v1/messages':
            raise llm.LLMError('Unexpected provider destination in isolated smoke')
        if len(calls) >= 3:
            raise llm.LLMError('Live smoke physical-request budget exhausted')
        calls.append('anthropic.messages')
        return actual_open(request, *args, **kwargs)

    result = {'status': 'failed', 'provider': 'anthropic', 'model': cfg.fast_model,
              'synthetic_data': True, 'live_slack': False, 'human_approval': False}
    try:
        with patch('urllib.request.urlopen', side_effect=budgeted_open):
            read = slack_chat.reading(cfg, {'question': 'Which usage is billable?',
                'message': 'Exclude the 600000 internal load-test calls. Keep real customer traffic billable.',
                'history': [], 'sources': [], 'pending_readback': None,
                'task': {'title': 'Synthetic usage billing test', 'repo': 'example/synthetic'}})
            result['slack_interpretation'] = {'kind': read.get('kind'), 'answer': read.get('answer', '')}
            if read.get('kind') != 'answer' or not read.get('answer', '').strip():
                result['reason'] = 'The live model did not produce a usable answer proposal'
                return result
            with tempfile.TemporaryDirectory(prefix='raven-live-smoke-') as directory:
                store = Store(Path(directory) / 'smoke.db')
                try:
                    person = store.graph.add_person('Synthetic Owner', email='owner@example.test')
                    owner = store.add_owner({'name': 'Synthetic Owner', 'team': 'Example', 'patterns': '*'})
                    task = store.add_run({'title': 'Synthetic timeout decision', 'repo': 'example/synthetic'})['id']
                    node = canvas.add_node(store, Config(model_api='none'), {'task_id': task,
                        'question': 'Which timeout should new clients use?', 'paths': 'src/client.py',
                        'context': 'Existing clients require compatibility.', 'owner_id': owner['id']})
                    actor = Actor(id=person, name='Synthetic Owner', kind='session')
                    draft = interview.create(store, task, {'decision_id': node['node_id'], 'client_key': 'smoke'}, actor)
                    draft = interview.update(store, task, draft['id'], {'expected_version': draft['version'],
                        'transcript': 'Use five seconds for new clients. Existing clients keep ten seconds. '
                                      'There is a migration cohort that may need an exception; ask me about that.',
                        'answer': '', 'rationale': ''}, actor)
                    guided = interview.advance(store, task, draft['id'], {'expected_version': draft['version']}, actor, cfg=cfg)
                    result['interview'] = guided['guidance']
                    result['decision_authorized'] = store.get_decision(node['node_id'])['authorized']
                    if guided['guidance'].get('mode') != 'model-assisted' or result['decision_authorized']:
                        result['reason'] = 'The model-guided draft failed validation or changed authorization'
                        return result
                    result['status'] = 'passed'
                    result['limits'] = 'Two synthetic model operations; this is not voice capture, an actual human decision, or a full live coding run.'
                finally:
                    store.graph.close()
    except Exception as error:
        # Error type only: provider error bodies may contain sensitive content.
        result['reason'] = type(error).__name__
    finally:
        result['physical_http_requests'] = len(calls)
        result['successful_completions'] = llm.USAGE.calls - before[0]
        result['input_tokens'] = llm.USAGE.input_tokens - before[1]
        result['output_tokens'] = llm.USAGE.output_tokens - before[2]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true', help='Explicitly authorize provider requests using the configured key')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = run() if args.run_live else {'status': 'blocked', 'reason': 'Pass --run-live only after secure provider setup and approval.'}
    text = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end='')
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
