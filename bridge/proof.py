"""Portable, content-addressed evidence for a submitted change.

The digest detects changed bytes; it is not a cryptographic signature by a
human. Human signoffs retain the identity and authority recorded by Raven.
Checks are host-reported claims, never relabeled as independently verified.
"""
import hashlib
import json

from .store import Invalid, field

SCHEMA = "raven-proof-v1"
LIMITATIONS = [
    "SHA-256 protects integrity, not the identity of the exporter or a human digital signature.",
    "Human signoffs and authority are assertions recorded by this Raven workspace.",
    "Test results are reported by the coding host; Raven did not independently execute them.",
    "A model diff review is advisory and does not prove code correctness.",
    "This bundle grants no authority for another task or a different scope.",
]


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _decisions(store, task_id):
    from . import canvas
    tree = canvas.get_tree(store, task_id)
    nodes = canvas._flatten(tree["nodes"])
    records = []
    for node in nodes:
        row = store.get_decision(node["node_id"])
        source_id = node.get("duplicate_of") or node["node_id"]
        source = store.get_decision(source_id) if source_id != node["node_id"] else row
        record = {k: node.get(k) for k in (
            "node_id", "parent_id", "question", "answer", "rationale", "evidence", "path", "category",
            "facts", "status", "authorized", "blocking", "needs_review", "signoff", "signed_by",
            "answered_by", "required_signers", "signatures", "rule_scope", "rule_conditions", "rule_expires",
            "reusable", "updated_at", "duplicate_of", "resolution")}
        record["answer_sha256"] = hashlib.sha256((node.get("answer") or "").encode()).hexdigest()
        record["attribution"] = {k: source.get(k) or "" for k in (
            "actor_id", "actor_name", "actor_basis", "signed_revision", "signed_hash", "answered_at")}
        record["source_decision_id"] = source_id
        record["source_revision"] = source.get("updated_at") or ""
        record['applicability'] = source.get('applicability') or ''
        record['authority_evidence'] = node.get('owner_evidence') or ''
        try:
            record['signature_records'] = json.loads(source.get('signatures') or '[]')
        except (TypeError, ValueError):
            record['signature_records'] = []
        record["citations"] = [{"kind": "decision", "id": source_id}]
        record["citations"].extend({"kind": link.get("kind"), "id": link.get("id"),
                                    "detail": link.get("detail", "")} for link in node.get("related", []))
        records.append(record)
    return tree, sorted(records, key=lambda item: item["node_id"])


def _latest(store, task_id):
    row = store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='proof_exported' "
                                 "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["detail"])["bundle"] if row else None


def _reported_checks(store, task_id):
    row = store.graph.db.execute("SELECT detail FROM events WHERE run_id=? AND kind='checks_reported' "
                                 "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(row["detail"]).get("checks", "") if row else ""


def create(store, task_id, diff, checks=None):
    """Snapshot a completed, unblocked task and the exact submitted diff."""
    from . import canvas
    run = canvas._task(store, task_id)
    if run["status"] != "completed":
        raise Invalid("Finish the task before exporting its change proof")
    if not isinstance(diff, str) or not diff.strip():
        raise Invalid("A complete unified diff is required for a change proof")
    if len(diff) > 60000:
        raise Invalid("The complete diff exceeds the proof limit of 60000 characters; do not submit an excerpt")
    tree, records = _decisions(store, task_id)
    if tree.get("needs_review") or tree['counts'].get('blocking') or any(n["blocking"] or n["needs_review"] for n in records):
        raise Invalid("The task has unresolved or corrected decisions; reread and finish it before exporting")
    payload = {
        "schema": SCHEMA,
        "task": {k: run[k] for k in ("id", "title", "repo", "requester", "goal", "status")},
        "change": {"diff": diff, "sha256": hashlib.sha256(diff.encode()).hexdigest(),
                   "bytes": len(diff.encode("utf-8"))},
        "decisions": records,
        "checks": {"provenance": "coding-host-reported", "independently_verified": False,
                   "text": _reported_checks(store, task_id) if checks is None else checks},
        "review": tree.get("review"),
        "limitations": LIMITATIONS,
    }
    bundle = {"id": _digest(payload), "algorithm": "sha256", "payload": payload}
    previous = _latest(store, task_id)
    if previous and previous.get("id") == bundle["id"]:
        return previous
    store.graph.append_event("proof_exported", {"task_id": task_id, "bundle": bundle})
    return bundle


def verify(bundle):
    """Offline integrity checks; a valid digest is not an authenticity claim."""
    errors = []
    if not isinstance(bundle, dict) or not isinstance(bundle.get("payload"), dict):
        return {"valid": False, "errors": ["Malformed proof bundle"]}
    payload = bundle["payload"]
    if payload.get("schema") != SCHEMA or bundle.get("algorithm") != "sha256":
        errors.append("Unsupported proof schema or algorithm")
    if bundle.get("id") != _digest(payload):
        errors.append("Proof digest does not match its contents")
    change = payload.get("change") or {}
    diff = change.get("diff")
    if not isinstance(diff, str) or hashlib.sha256(diff.encode()).hexdigest() != change.get("sha256"):
        errors.append("Submitted diff digest does not match")
    elif len(diff.encode()) != change.get("bytes"):
        errors.append("Submitted diff length does not match")
    for node in payload.get("decisions") or []:
        if hashlib.sha256((node.get("answer") or "").encode()).hexdigest() != node.get("answer_sha256"):
            errors.append("Decision answer digest does not match: " + str(node.get("node_id")))
    return {"valid": not errors, "errors": errors, "authenticity_verified": False}


def export(store, data):
    """Return the saved finish proof, with an explicit live staleness check."""
    from . import canvas
    task_id = field(data, "task_id", limit=100)
    run = canvas._task(store, task_id)
    bundle = _latest(store, task_id)
    if bundle is None:
        raise Invalid("No change proof saved; call bridge_finish_task with the complete diff first")
    tree, records = _decisions(store, task_id)
    reasons = []
    if records != bundle["payload"]["decisions"]:
        reasons.append("A decision, signature, authority, or scope changed since this proof")
    if run["status"] != "completed" or tree.get("needs_review"):
        reasons.append("The task is no longer completed and current")
    review = tree.get("review")
    review_pending = bool(review and review.get("status") == "running")
    review_snapshot_current = review == bundle["payload"].get("review")
    if reasons:
        next_step = "Reread the tree and finish with the current complete diff"
    elif review_pending:
        next_step = ("The advisory review is still running. Call bridge_wait; once it finishes, call "
                     "bridge_finish_task again with the same complete diff and checks to refresh the saved proof, "
                     "then export again. The current bundle is an immutable earlier snapshot.")
    elif review and review.get("status") == "failed":
        next_step = ("The advisory review failed. Call bridge_finish_task again with the same complete diff "
                     "and checks to retry it, then export the updated proof. The current bundle preserves "
                     "the failed attempt and does not establish conformance.")
    elif not review_snapshot_current:
        next_step = ("The saved proof contains an earlier advisory review snapshot. Read review, then call "
                     "bridge_finish_task again with the same complete diff and checks to save an updated proof "
                     "and export again. The current bundle has not been rewritten.")
    else:
        next_step = "Attach the bundle and summary to the code review; review and tests remain required"
    return {"bundle": bundle, "integrity": verify(bundle), "stale": bool(reasons),
            "stale_reasons": reasons, "markdown": markdown(bundle), "review": review,
            "review_pending": review_pending, "review_snapshot_current": review_snapshot_current,
            "next": next_step}


def markdown(bundle):
    """A review-ready summary; source text is quoted, never executable markup."""
    payload = bundle["payload"]
    def quote(text):
        return "\n".join("> " + line for line in str(text or "").splitlines()) or "> (none)"
    lines = ["# Raven change proof", "", "Task: " + payload["task"]["id"],
             "Repository: " + payload["task"]["repo"], "", quote(payload["task"]["title"]), "",
             "Proof SHA-256: " + bundle["id"], "Diff SHA-256: " + payload["change"]["sha256"],
             "", "## Decision record"]
    for node in payload["decisions"]:
        lines.extend(["", "### " + node["node_id"], quote(node["question"]), "", quote(node["answer"]),
                      "", "Recorded signoff: " + str(node["signoff"] or "none"),
                      quote("By: " + (node["signed_by"] or node["answered_by"] or "not recorded")),
                      quote("Scope: " + json.dumps(node["facts"], sort_keys=True)),
                      quote("Evidence: " + (node["evidence"] or "No external evidence recorded"))])
    lines.extend(["", "## Host-reported checks", quote(payload["checks"]["text"]), "",
                  "## Trust limits", *["- " + text for text in payload["limitations"]]])
    return "\n".join(lines) + "\n"


def main():
    """Verify the bundle bytes and optionally compare the actual review diff."""
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Check Raven change-proof integrity; does not verify authenticity')
    parser.add_argument('bundle', help='Saved proof JSON or bridge_export_proof result JSON')
    parser.add_argument('--diff', help='Actual complete patch to compare with the proof')
    args = parser.parse_args()
    try:
        saved = json.loads(Path(args.bundle).read_text())
        bundle = saved.get('bundle', saved) if isinstance(saved, dict) else saved
        result = verify(bundle)
        if args.diff:
            digest = hashlib.sha256(Path(args.diff).read_bytes()).hexdigest()
            expected = ((bundle or {}).get('payload') or {}).get('change', {}).get('sha256')
            result['diff_matches'] = digest == expected
            if not result['diff_matches']:
                result['valid'] = False
                result['errors'].append('The actual patch does not match the recorded diff')
    except (OSError, ValueError, TypeError) as error:
        result = {'valid': False, 'errors': [str(error)], 'authenticity_verified': False}
    print(json.dumps(result, indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
