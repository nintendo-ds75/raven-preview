"""Whether a follow-up a person adds holds the task: optional (the default)
never does; one marked required does until the agent adopts it and it is
answered. The reproduction for H5 in the connector review of 35a5f84."""
import json
import os
import tempfile
from pathlib import Path

os.environ["BRIDGE_MODEL_API"] = "none"
os.environ["BRIDGE_SEMANTIC"] = "0"

from bridge import canvas  # noqa: E402
from bridge.authz import Actor  # noqa: E402
from bridge.config import Config  # noqa: E402
from bridge.store import Invalid, Store  # noqa: E402

QUESTION = "Before release, which option will handle redirects: apply jitter there too, or explicitly exclude redirects?"


def finish(store, task):
    try:
        return canvas.finish_task(store, {"task_id": task})["status"]
    except Invalid as error:
        return f"refused: {error}"


def run(required: bool) -> dict:
    with tempfile.TemporaryDirectory() as temp:
        store = Store(Path(temp) / "probe.db")
        graph, cfg, repo = store.graph, Config(model_api="none"), "evaluation/followup"
        with graph.transaction():
            person = graph.add_person("Runtime Owner", email="runtime@followup.invalid")
            graph.add_authority("repo", "", "decides", person_id=person, repo=repo, source="config",
                                asserted_by="operator", accepted=True)
        actor = Actor.person(graph.get_person(person))
        task = canvas.start_task(store, cfg, {"title": "Choose default retry policy", "repo": repo,
                                              "paths": "src/retry.py"})["task_id"]
        node = canvas.add_node(store, cfg, {"task_id": task, "paths": "src/retry.py",
                                            "question": "Must the optional policy preserve defaults?"})
        store.answer(node["node_id"], {"answer": "Yes, preserve defaults.", "rationale": "Compatibility."}, actor=actor)
        added = canvas.add_followups(store, cfg, node["node_id"], {"questions": [QUESTION], "required": required},
                                     actor=actor)["nodes"][0]
        out = {"required": required, "followup_blocking": added["blocking"], "next": added["next"],
               "finish_left_unadopted": finish(store, task)}
        if required:
            adopted = canvas.add_node(store, cfg, {"task_id": task, "question": QUESTION, "adopt": added["node_id"]})
            out["finish_adopted_unanswered"] = finish(store, task)
            store.answer(adopted["node_id"], {"answer": "Exclude redirects.", "rationale": "A separate change."},
                         actor=actor)
            out["finish_answered"] = finish(store, task)
        return out


if __name__ == "__main__":
    print(json.dumps([run(False), run(True)], indent=2))
