"""Offline trace viewer: recorded observations at a point in time, never live state."""
import json
from pathlib import Path

from .ledger import Ledger, verify


def render(ledger, output, *, expected_head=None):
    rows = Ledger(ledger).read(expected_head)
    data = json.dumps({'head': verify(rows), 'events': rows}, ensure_ascii=False).replace('<', '\\u003c')
    template = Path(__file__).with_name('viewer.html').read_text()
    logic = Path(__file__).with_name('viewer-data.js').read_text()
    Path(output).write_text(template.replace('/*AUDIT_LOGIC*/', logic).replace('/*AUDIT_DATA*/null', data))
    return {'events': len(rows), 'head': verify(rows), 'output': str(output)}
