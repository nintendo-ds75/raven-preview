"""Executable demo contract, separate from Raven's unfinished coding fixture.

This reference implements explicit synthetic review decisions used by the
acceptance tests. It is not a production billing engine or a claim that any
real finance/legal owner approved these policies.
"""
from collections import defaultdict
from datetime import datetime


def invoice(records, *, free_per_seat=500, overage_cents=2, legal_holds=frozenset()):
    """Nonpooled monthly allowance per seat; no rollover; trusted internal
    load-test events are excluded. Legal holds defer invoicing, never usage.

    Account, seat and origin must come from the trusted metering pipeline.
    This sample does not authenticate usage events or collect money.
    """
    for value in (free_per_seat, overage_cents):
        if type(value) is not int or value < 0:
            raise ValueError('Allowance and rate must be nonnegative integers')
    usage, internal = defaultdict(int), defaultdict(int)
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Each usage event must be an object')
        account, seat = record.get('account'), record.get('seat')
        if not isinstance(account, str) or not account or not isinstance(seat, str) or not seat:
            raise ValueError('Stable account and seat identifiers are required')
        units = record.get('units')
        if type(units) is not int or units < 0:
            raise ValueError('Usage units must be a nonnegative integer')
        try:
            at = datetime.fromisoformat(record['at'].replace('Z', '+00:00'))
        except (KeyError, AttributeError, TypeError, ValueError) as error:
            raise ValueError('Usage requires an ISO timestamp') from error
        if at.tzinfo is None:
            raise ValueError('Usage timestamp requires an explicit timezone')
        from datetime import timezone
        month = at.astimezone(timezone.utc).strftime('%Y-%m')
        key = account, month, seat
        origin = record.get('origin', 'customer')
        if origin not in ('customer', 'internal_load_test'):
            raise ValueError('Unrecognized trusted usage origin')
        if origin == 'internal_load_test':
            internal[key] += units
            usage[key] += 0
        else:
            usage[key] += units
    accounts = {}
    for (account, month, seat), units in sorted(usage.items()):
        key = account + ':' + month
        row = accounts.setdefault(key, {'account': account, 'month': month, 'usage_allowed': True,
                                       'billable_units': 0, 'excluded_internal_units': 0,
                                       'overage_units': 0, 'amount_cents': 0, 'deferred_cents': 0,
                                       'legal_hold': account in legal_holds})
        overage = max(0, units - free_per_seat)
        row['billable_units'] += units
        row['excluded_internal_units'] += internal[(account, month, seat)]
        row['overage_units'] += overage
        row['deferred_cents' if row['legal_hold'] else 'amount_cents'] += overage * overage_cents
    return list(accounts.values())
