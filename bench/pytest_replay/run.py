"""Curated pytest canvas smoke replay; no model calls or upstream writes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import time

from bridge import canvas
from bridge.config import load
from bridge.ingest import index_repo
from bridge.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    cases = json.loads(Path(__file__).with_name('cases.json').read_text())
    # Never reuse a database carrying answers from another run.
    args.out.mkdir(parents=True, exist_ok=False)
    os.environ.update(BRIDGE_SEMANTIC='0', BRIDGE_MODEL_API='none', BRIDGE_LIVE='0')
    cfg = load()
    results = []
    for case in cases:
        start = time.monotonic()
        print(f"PR {case['pr']}: ingesting {case['base']}", flush=True)
        subprocess.run(['git', '-C', str(args.repo), 'cat-file', '-e', case['base'] + '^{commit}'], check=True)
        store = Store(args.out / f"{case['pr']}.db")
        try:
            stats = index_repo(store.graph, args.repo, repo_name='pytest', rev=case['base'], max_commits=2000)
            task = canvas.start_task(store, cfg, {
                'title': case['title'], 'goal': case['goal'], 'repo': 'pytest-dev/pytest',
                'agent': 'Curated deterministic replay', 'requester': case['requester'],
                'paths': case['paths'],
            })
            nodes = []
            # These are independent diagnostic probes, not an agent-discovered
            # tree. Probe even a pass verdict, and expose that in the report.
            for i, question in enumerate(case['questions']):
                data = dict(question, task_id=task['task_id'], client_ref=f'probe-{i}')
                node = canvas.add_node(store, cfg, data)
                repeated = canvas.add_node(store, cfg, data)
                assert repeated['node_id'] == node['node_id'], 'Retry created a second node'
                nodes.append(node)
            result = {'case': case, 'ingest': stats, 'task': task, 'nodes': nodes,
                      'tree': canvas.get_tree(store, task['task_id']),
                      'seconds': round(time.monotonic() - start, 2)}
            results.append(result)
            (args.out / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
            print(f"PR {case['pr']}: {task['verdict']}; " +
                  ', '.join(f"{n['status']} → {n['owner']}" for n in nodes), flush=True)
        finally:
            store.graph.close()


if __name__ == '__main__':
    main()
