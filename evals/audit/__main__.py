"""python -m evals.audit --help"""
import argparse
import json
from pathlib import Path

from .fixture import attest_patch, freeze, verify_fixture
from .ledger import Ledger
from .scoring import pin_rubric, score_run
from .viewer import render


def load(path):
    return json.loads(Path(path).read_text())


def main():
    p = argparse.ArgumentParser(description='Freeze replay inputs, record observations, and inspect the evaluation.')
    sub = p.add_subparsers(dest='command', required=True)
    f = sub.add_parser('freeze')
    for name in ('repo', 'baseline', 'cutoff', 'out', 'brief', 'brief-provenance'):
        f.add_argument('--' + name, required=True)
    f.add_argument('--records')
    v = sub.add_parser('verify-fixture'); v.add_argument('fixture')
    r = sub.add_parser('record')
    for name in ('ledger', 'kind', 'data', 'actor'):
        r.add_argument('--' + name, required=True)
    v = sub.add_parser('view')
    v.add_argument('--ledger', required=True); v.add_argument('--out', required=True); v.add_argument('--head')
    a = sub.add_parser('attest-patch')
    for name in ('repo', 'baseline', 'patch', 'ledger'):
        a.add_argument('--' + name, required=True)
    s = sub.add_parser('score')
    for name in ('rubric', 'observations', 'ledger'):
        s.add_argument('--' + name, required=True)
    s = sub.add_parser('pin-rubric')
    for name in ('rubric', 'cutoff', 'ledger'):
        s.add_argument('--' + name, required=True)
    args = p.parse_args()
    if args.command == 'freeze':
        result = freeze(args.repo, args.baseline, args.cutoff, args.out, Path(args.brief).read_text(),
                        records=load(args.records) if args.records else [], brief_provenance=load(args.brief_provenance))
    elif args.command == 'verify-fixture':
        result = verify_fixture(args.fixture)
    elif args.command == 'record':
        result = Ledger(args.ledger).append(args.kind, load(args.data), actor=args.actor)
    elif args.command == 'view':
        result = render(args.ledger, args.out, expected_head=args.head)
    elif args.command == 'attest-patch':
        result = attest_patch(args.repo, args.baseline, Path(args.patch).read_bytes())
        Ledger(args.ledger).append('patch_checked', result)
    elif args.command == 'pin-rubric':
        result = pin_rubric(Ledger(args.ledger), load(args.rubric), args.cutoff)
    else:
        rubric = load(args.rubric)
        result = score_run(Ledger(args.ledger), rubric, load(args.observations))
        Ledger(args.ledger).append('evaluation_scored', result)
    print(json.dumps(result, indent=2))
    if args.command == 'attest-patch' and not result['pass']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
