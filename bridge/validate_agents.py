"""Repeatable contract/access/continuation probe; live work is explicitly selected.

Run: python -m bridge.validate_agents --help
The checkpoint contains public session identities and synthetic fixture data, never keys.
"""

import argparse
import base64
import inspect
import json
import re
import time
import uuid
from pathlib import Path

from .agents_api import AgentsAPI, ConfigurationError, SDK_VERSION, create_client, result_event, safe_error
from .store import now

PROBE_TOOL = {"type": "function", "name": "request_judgment",
              "description": "Wait for an externally supplied validation decision.",
              "parameters": {"type": "object", "properties": {"question": {"type": "string"}},
                             "required": ["question"], "additionalProperties": False}}


def sdk_contract():
    import openai
    if openai.__version__ != SDK_VERSION:
        raise ConfigurationError(f"Install openai=={SDK_VERSION}")
    # This client has a dummy key and makes no requests.
    with openai.OpenAI(api_key="contract-only", max_retries=0) as client:
        sessions = client.beta.agents.sessions
        checks = [(sessions.create, ("environment", "agent", "input", "metadata")),
                  (sessions.retrieve, ("session_id",)), (sessions.list, ("after", "order")),
                  (sessions.events.create, ("session_id", "events", "idempotency_key")),
                  (sessions.items.list, ("session_id", "after", "order")),
                  (sessions.turns.list, ("session_id",)),
                  (sessions.artifacts.content, ("session_id", "artifact_id"))]
        for method, names in checks:
            if not set(names) <= set(inspect.signature(method).parameters):
                raise ConfigurationError(f"SDK contract changed: {method.__qualname__}")
    return {"sdk": SDK_VERSION, "contract": "passed", "live": False}


class Probe:
    def __init__(self, api, path, model, timeout=180, delay=10):
        self.api, self.path, self.model = api, Path(path), model
        self.timeout, self.delay = timeout, delay
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "probe_id": uuid.uuid4().hex, "sdk": SDK_VERSION, "model": model,
            "started_at": now(), "stages": {}, "trace": []}
        if self.data["model"] != model or self.data["sdk"] != SDK_VERSION:
            raise ConfigurationError("Use the checkpoint's model and SDK, or a new checkpoint path")

    def save(self, event, **detail):
        self.data["trace"].append({"at": now(), "event": event, **detail})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.data, indent=2) + "\n")
        temp.replace(self.path)
        print(json.dumps({"event": event, **detail}), flush=True)

    def session(self, name, environment, task, tools=()):
        stage = self.data["stages"].setdefault(name, {})
        if stage.get("deleted") and not stage.get("passed"):
            raise ConfigurationError("This probe session was deleted; use a new --report path for a new trial")
        if stage.get("session_id"):
            return stage
        metadata = {"bridge_probe": self.data["probe_id"], "stage": name}
        if stage.get("launch_intent"):
            matches = self.api.find_sessions(metadata)
            if len(matches) != 1:
                raise ConfigurationError("Ambiguous launch: no unique session recovered; do not blindly relaunch")
            session = matches[0]
        else:
            stage["launch_intent"] = True
            self.save("launch_intent", stage=name)
            session = self.api.start_task(model=self.model, instructions="Follow the validation task precisely.",
                                          tools=list(tools), environment=environment, task=task, metadata=metadata)
        stage["session_id"] = session["id"]
        stage["environment"] = session["environment"]
        self.save("session_linked", stage=name, session_id=session["id"])
        return stage

    def wait_for(self, session_id, predicate):
        deadline, last = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            session = self.api.retrieve_session(session_id)
            if session["status"] != last:
                last = session["status"]
                self.save("session_state", session_id=session_id, status=last)
            if last == "failed":
                raise RuntimeError("Provider session failed; inspect the session using its saved ID")
            value = predicate(session)
            if value:
                return value
            time.sleep(2)
        raise TimeoutError("Validation deadline reached; rerun using the same checkpoint")

    def completed_turn(self, session_id, exclude=()):
        def check(session):
            turns = [t for t in self.api.turns(session_id)
                     if not t.get("subagent_id") and t["id"] not in exclude]
            if not turns:
                return None
            turn = turns[-1]
            if turn["status"] in ("failed", "cancelled"):
                raise RuntimeError(f"Validation turn {turn['status']}")
            if turn["status"] == "completed" and not session["required_actions"]:
                return turn
        return self.wait_for(session_id, check)

    def function(self):
        stage = self.session("function", {"type": "none"},
                             "Call request_judgment with question 'Validation decision?'. "
                             "Wait for its actual result, then reply exactly with the receipt value in the result.",
                             [PROBE_TOOL])
        if stage.get("passed"):
            return
        sid = stage["session_id"]
        if not stage.get("action"):
            action = self.wait_for(sid, lambda s: next((a for a in s["required_actions"]
                         if a["type"] == "function_call" and a["name"] == "request_judgment"), None))
            stage["action"] = action
            stage["receipt"] = uuid.uuid4().hex
            self.save("function_pending", session_id=sid, call_id=action["call_id"], turn_id=action["turn_id"])
        action = stage["action"]
        if not stage.get("delivery_intent"):
            time.sleep(self.delay)
            session = self.api.retrieve_session(sid)
            if action not in session["required_actions"]:
                raise RuntimeError("Function did not remain pending during the review delay")
            stage["delay_verified_seconds"] = self.delay
            stage["delivery_intent"] = True
            self.save("review_delay_verified", seconds=self.delay)
        event = result_event(action, {"receipt": stage["receipt"], "answer": "Do not perform a billing change.",
                                    "provenance": "synthetic validation result; no human approval"})
        outputs = [i for i in self.api.items(sid) if i["type"] == "function_call_output"
                   and i["call_id"] == action["call_id"] and i["turn_id"] == action["turn_id"]]
        if not outputs:
            pending = self.api.retrieve_session(sid)["required_actions"]
            if action not in pending:
                raise RuntimeError("Delivery outcome is uncertain; call absent without saved output")
            self.api.send_events(sid, [event], self.data["probe_id"] + "-result")
            self.save("result_accepted", session_id=sid)
        turn = self.completed_turn(sid)
        items = self.api.items(sid)
        replies = [i for i in items if i["type"] == "message" and i.get("role") == "assistant"
                   and i["turn_id"] == action["turn_id"]]
        if not any(stage["receipt"] in json.dumps(i.get("content")) for i in replies):
            raise RuntimeError("The completed turn did not include the delivered receipt")
        if turn["id"] != action["turn_id"]:
            raise RuntimeError("Result did not continue the original turn")
        stage["passed"] = True
        stage["turn"] = turn
        stage["items"] = items
        self.save("function_continuation_passed", session_id=sid, turn_id=turn["id"])

    def hosted(self):
        marker = self.data["probe_id"]
        environment = {"type": "openai_hosted", "network": {"access": "disabled"},
                       "files": [{"type": "inline", "path": "/workspace/marker.txt",
                                  "data": base64.b64encode(marker.encode()).decode()}]}
        stage = self.session("hosted", environment,
                             "Use Python to read /workspace/marker.txt. Write JSON with key marker and "
                             "that value to /workspace/outputs/validation.json. Run a Python assertion "
                             "that the written value equals the input. Report actual command output.")
        if stage.get("passed"):
            return
        sid = stage["session_id"]
        if not stage.get("first_turn"):
            stage["first_turn"] = self.completed_turn(sid)
            self.save("hosted_first_turn_completed", session_id=sid)
        if not stage.get("followup_intent"):
            stage["followup_intent"] = True
            self.save("hosted_followup_intent", session_id=sid)
            self.api.send_events(sid, [{"type": "agent.session.input.message", "input": [{"role": "user",
                "content": [{"type": "input_text", "text": "Read the existing marker.txt again, assert it "
                    "equals outputs/validation.json's marker, and write the same JSON to "
                    "/workspace/outputs/continued.json. Report the actual assertion result."}]}]}],
                self.data["probe_id"] + "-hosted-followup")
        turn = self.completed_turn(sid, [stage["first_turn"]["id"]])
        artifacts = self.api.artifacts(sid)
        for turn_id, name in [(stage["first_turn"]["id"], "validation.json"), (turn["id"], "continued.json")]:
            artifact = next((a for a in artifacts if a["turn_id"] == turn_id
                             and a["path"] == "/workspace/outputs/" + name), None)
            if not artifact:
                raise RuntimeError("Required validation artifact is absent")
            content = self.api.artifact_content(sid, artifact["id"])
            if json.loads(content) != {"marker": marker}:
                raise RuntimeError("Artifact marker differs from original input")
            (self.path.parent / name).write_bytes(content)
        stage.update(passed=True, second_turn=turn, artifacts=artifacts, items=self.api.items(sid))
        self.save("hosted_continuation_and_artifacts_passed", session_id=sid)

    def workflow(self):
        """Real provider, synthetic local review; never writes to the user's inbox DB."""
        from .execution import ExecutionService
        from .store import Store
        stage = self.data["stages"].setdefault("workflow", {})
        if stage.get("passed"):
            self.save("workflow_previously_validated", session_id=stage["session_id"])
            return
        if stage.get("deleted"):
            raise ConfigurationError("This workflow session was deleted; use a new --report path")
        store = Store(self.path.parent / "workflow.db")
        if not store.state()["owners"]:
            store.add_owner({"name": "Synthetic validation owner", "team": "Fixture billing", "patterns": "billing/*"})
            prior_run = store.add_run({"title": "Synthetic precedent for a different test", "repo": "billing-fixture"})
            decision = store.request({"run_id": prior_run["id"], "question": "Should the load-test usage spike be billed?",
                "context": "A different account, August trial spike-august-1; this approval applies only to that trial.",
                "path": "billing/usage.py"})
            store.answer(decision["id"], {"answer": "Exclude only spike-august-1 for the August trial.",
                "rationale": "Synthetic precedent, not approval for September traffic."})
            store.update_run(prior_run["id"], {"status": "completed"})
        service = ExecutionService(store, self.api, self.model)
        result = service.submit({"task": "Add usage-based pricing", "repository": "billing-fixture",
                                 "submission_key": self.data["probe_id"] + "-workflow"})
        stage["run_id"] = result["id"]
        self.save("workflow_registered", run_id=result["id"])
        deadline, last = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            service.tick()
            run = service.get(stage["run_id"])
            if run["status"] != last:
                last = run["status"]
                self.save("workflow_state", run_id=stage["run_id"], status=last, session_id=run["session_id"])
            state = store.state(full_history=True)
            pending = [d for d in state["decisions"] if d["run_id"] == stage["run_id"] and d["status"] == "pending"]
            if pending and not stage.get("answered"):
                if len(pending) != 1:
                    raise RuntimeError("Inspect multiple live questions before continuing validation")
                question = pending[0]
                stage["question"] = question
                stage["pending_items"] = self.api.items(run["session_id"])
                self.save("workflow_judgment_pending", question=question["question"], context=question["context"])
                time.sleep(self.delay)
                later = self.api.items(run["session_id"])
                before_ids = {i.get("id") for i in stage["pending_items"]}
                stage["independent_items_while_pending"] = [i for i in later if i.get("id") not in before_ids]
                # Reopen the DB/adapter before review, then again before delivery.
                store = Store(store.path)
                store.answer(question["id"], {"answer": "For this disposable fixture, exclude only records tagged "
                    "internal-load-test with test_run spike-september-13. Bill all other usage normally. "
                    "Add tests for both the exact exclusion and other tagged traffic that must still be billed.",
                    "rationale": "Synthetic validation answer for this fixture; not an actual billing policy.",
                    "expected_updated_at": question["updated_at"]})
                service = ExecutionService(Store(store.path), self.api, self.model)
                stage["answered"] = True
                self.save("workflow_synthetic_answer_saved", decision_id=question["id"])
            elif pending and stage.get("answered"):
                raise RuntimeError("Additional live question requires inspection")
            if run["status"] in ("failed", "cancelled", "recovery_required", "review_required"):
                raise RuntimeError("Workflow stopped; inspect the saved run")
            if run["status"] == "result_ready":
                if not stage.get("answered"):
                    raise RuntimeError("Agent missed the unresolved billing judgment")
                snapshot = json.loads(run["snapshot"])
                commands = [i for i in snapshot["items"] if i["type"] == "command_execution"]
                if not any("unittest" in i.get("command", "") and i.get("status") == "completed"
                           and (i.get("exit_code") == 0 or (i.get("exit_code") is None
                                and re.search(r"Ran [1-9][0-9]* tests? in .*\n\s*\nOK\b", i.get("output") or "")))
                           for i in commands):
                    raise RuntimeError("No successful actual test command found")
                downloaded = {}
                for artifact in snapshot.get("artifacts", []):
                    name = artifact["path"].rsplit("/", 1)[-1]
                    if name not in ("changes.patch", "result.json"):
                        continue
                    content = self.api.artifact_content(run["session_id"], artifact["id"])
                    (self.path.parent / name).write_bytes(content)
                    downloaded[name] = artifact
                if set(downloaded) != {"changes.patch", "result.json"}:
                    raise RuntimeError("Expected patch and result artifacts are missing")
                stage.update(passed=True, session_id=run["session_id"], snapshot=snapshot,
                             artifacts=downloaded, deliveries=state["deliveries"])
                (self.path.parent / "workflow-export.json").write_text(json.dumps(state, indent=2) + "\n")
                self.save("workflow_integration_passed", session_id=run["session_id"],
                          independent_items=len(stage["independent_items_while_pending"]))
                return
            time.sleep(3)
        raise TimeoutError("Workflow validation deadline reached; resume with the same checkpoint")

    def retry_result(self):
        stage = self.data["stages"].get("function", {})
        if not stage.get("passed") or stage.get("deleted"):
            raise ConfigurationError("Run the function probe before retry validation")
        sid, action = stage["session_id"], stage["action"]
        event = result_event(action, {"receipt": stage["receipt"], "answer": "Do not perform a billing change.",
                                    "provenance": "synthetic validation result; no human approval"})
        before = self.api.items(sid)
        try:
            self.api.send_events(sid, [event], self.data["probe_id"] + "-result")
            stage["duplicate_result_http"] = "accepted"
        except Exception as error:
            stage["duplicate_result_http"] = safe_error(error)
        after = self.api.items(sid)
        outputs = [i for i in after if i["type"] == "function_call_output" and i["call_id"] == action["call_id"]]
        stage["duplicate_result_count"] = len(outputs)
        stage["duplicate_result_new_item_count"] = len(after) - len(before)
        self.save("result_retry_observed", response=stage["duplicate_result_http"],
                  output_count=len(outputs), new_items=len(after) - len(before))
        if len(outputs) != 1 or len(after) != len(before):
            raise RuntimeError("Result retry created additional history; inspect provider behavior")

    def cleanup(self):
        for name, stage in self.data["stages"].items():
            sid = stage.get("session_id")
            if not sid or stage.get("deleted"):
                continue
            try:
                session = self.api.retrieve_session(sid)
            except Exception as error:
                if getattr(error, "status_code", None) == 404:
                    stage["deleted"] = True
                    self.save("validation_session_already_deleted", stage=name, session_id=sid)
                    continue
                raise
            if session["status"] in ("in_progress", "requires_action"):
                self.api.cancel(sid, self.data["probe_id"] + "-cleanup-" + name)
            for attempt in range(5):
                try:
                    self.api.delete(sid)
                    stage["deleted"] = True
                    self.save("validation_session_deleted", stage=name, session_id=sid)
                    break
                except Exception as error:
                    if getattr(error, "status_code", None) == 404:
                        stage["deleted"] = True
                        self.save("validation_session_already_deleted", stage=name, session_id=sid)
                        break
                    if getattr(error, "status_code", None) != 409 or attempt == 4:
                        raise
                    time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", choices=["access", "function", "hosted", "workflow", "retry", "cleanup", "all"], help="Explicitly make API requests; execution probes incur usage")
    parser.add_argument("--model", default="gpt-6-astra")
    parser.add_argument("--api-key-file", help="Read a key from this local file without printing it")
    parser.add_argument("--report", default=".bridge/validation/report.json")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--delay", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.delay <= 60 or not 10 <= args.timeout <= 600:
        parser.error("delay must be 1–60 seconds and timeout 10–600 seconds")
    probe = None
    try:
        print(json.dumps(sdk_contract()), flush=True)
        if not args.live:
            return 0
        with create_client(args.api_key_file) as client:
            api = AgentsAPI(client)
            probe = Probe(api, args.report, args.model, args.timeout, args.delay)
            # Read-only account access check before creating billable resources.
            client.beta.agents.sessions.list(limit=1)
            probe.save("account_read_access_passed")
            if args.live in ("function", "all"):
                probe.function()
            if args.live in ("hosted", "all"):
                probe.hosted()
            if args.live == "workflow":
                probe.workflow()
            if args.live == "retry":
                probe.retry_result()
            if args.live == "cleanup":
                probe.cleanup()
        return 0
    except Exception as error:
        detail = safe_error(error)
        if isinstance(error, ConfigurationError):
            detail["message"] = str(error)
        if probe:
            probe.save("validation_stopped", error=detail)
        else:
            print(json.dumps({"event": "validation_stopped", "error": detail}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
