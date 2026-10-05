"""Reproduce a docs referral granting unrelated security decision authority.

Run from the repository root: python -m evals.newdev.handoff_scope_probe
No network, model, real accounts or persistent database. This is a diagnostic,
not an assertion that the observed authority expansion is desirable.
"""
import json
import os
import tempfile
from pathlib import Path

os.environ["BRIDGE_MODEL_API"] = "none"
os.environ["BRIDGE_SEMANTIC"] = "0"

from bridge import canvas
from bridge.authz import Actor, basis_for
from bridge.config import Config
from bridge.store import Store


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "probe.db")
        graph = store.graph
        cfg = Config(model_api="none")
        repo = "evaluation/library"
        with graph.transaction():
            library = graph.add_person("Library Owner", email="library@example.invalid")
            release = graph.add_person("Release Owner", email="release@example.invalid")
            graph.add_authority("repo", "", "decides", person_id=library, repo=repo,
                                source="config", asserted_by="evaluation", accepted=True)
        actor = Actor.person(graph.get_person(release))
        task = canvas.start_task(store, cfg, {
            "title": "Review release notes", "repo": repo, "client_key": "scope-probe",
        })["task_id"]
        security = canvas.add_node(store, cfg, {
            "task_id": task, "question": "May TLS certificate verification be bypassed after a security validation error?",
            "paths": "src/ssl.py", "category": "security", "client_ref": "runtime",
        })
        decision = store.get_decision(security["node_id"])
        before = basis_for(graph, actor, decision, "answer")
        docs = canvas.add_node(store, cfg, {
            "task_id": task,
            "question": "Should the release notes call the X-Api-Key redirect change a security fix, or describe it as compatibility hardening?",
            "context": "Only public wording is being decided. No code or policy change.",
            "paths": "docs/guide.rst", "category": "docs", "client_ref": "wording",
        })
        handoff = store.refer(docs["node_id"], {"person": release},
                              actor=Actor.person(graph.get_person(library)))
        store.answer(docs["node_id"], {
            "answer": "Describe credential-leak prevention with a compatibility note.",
            "rationale": "Describe the narrow default change accurately.",
        }, actor=actor)
        after = basis_for(graph, actor, decision, "answer")
        print(json.dumps({
            "before": {"basis": before[0], "reason": before[1]},
            "learned": handoff["learned"],
            "after": {"basis": after[0], "reason": after[1]},
            "unexpected_security_authority": not before[0] and after[0] == "authority",
        }, indent=2))


if __name__ == "__main__":
    main()
