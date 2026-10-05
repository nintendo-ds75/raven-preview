"""Optional, explicit boundary to the managed Agents API (not the Agents SDK)."""

import json
import os
from pathlib import Path

SDK_VERSION = "3.13.0"
INSTRUCTION_VERSION = "bridge-judgment-v1"
INSTRUCTIONS = """Investigate code, documentation, and prior decisions before asking a person.
Make routine implementation choices yourself. When a consequential choice about intent,
policy, ownership, or tradeoffs remains unresolved, use request_judgment. Explain the
evidence, remaining uncertainty, alternatives, and consequences. Prior decisions are
evidence for their original circumstances; a suggested answer is not approval for this
request. Do not invent a policy to unblock yourself. Incorporate the reviewed answer
and its conditions into the original task, verify the work, and report limitations.
Keep dependent work pending until the actual reviewed answer arrives.
"""


class ConfigurationError(ValueError):
    pass


def safe_error(error):
    """Do not persist response bodies, headers, keys, or arbitrary exception text."""
    result = {"type": type(error).__name__}
    for key in ("status_code", "request_id"):
        value = getattr(error, key, None)
        if isinstance(value, (int, str)):
            result[key] = value
    return result


def create_client(api_key_file=None):
    try:
        import openai
    except ImportError as error:
        raise ConfigurationError("Install requirements-agents.txt in an isolated Python environment") from error
    if openai.__version__ != SDK_VERSION:
        raise ConfigurationError(f"Expected openai=={SDK_VERSION}; found {openai.__version__}")
    key = Path(api_key_file).read_text().strip() if api_key_file else os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ConfigurationError("Set OPENAI_API_KEY or supply --api-key-file (never put a key in an argument)")
    # Disable hidden retries; durable application state owns mutation recovery.
    # Do not inherit OPENAI_BASE_URL and accidentally send this key elsewhere.
    return openai.OpenAI(api_key=key, base_url="https://api.openai.com/v1", max_retries=0, timeout=30,
                         webhook_secret=os.environ.get("OPENAI_WEBHOOK_SECRET"))


def as_dict(value):
    return value if isinstance(value, dict) else value.to_dict()


def result_event(action, output=None, error=None):
    if action.get("type") != "function_call":
        raise ValueError("Only a pending function call can receive a tool result")
    if not all(isinstance(action.get(key), str) and action[key] for key in ("turn_id", "call_id")):
        raise ValueError("A function result requires its turn and call IDs")
    event = {"type": "agent.session.input.tool_result", "turn_id": action["turn_id"],
             "call_id": action["call_id"], "success": error is None}
    if error is None:
        event["output"] = json.dumps(output, sort_keys=True)
    else:
        event["error"] = error
    return event


class AgentsAPI:
    def __init__(self, client):
        self.client = client
        self.sessions = client.beta.agents.sessions

    def start_task(self, *, model, instructions, tools, environment, task, metadata):
        return as_dict(self.sessions.create(agent={"model": model, "instructions": instructions,
                      "tools": tools}, environment=environment, input=task, metadata=metadata))

    def retrieve_session(self, session_id):
        return as_dict(self.sessions.retrieve(session_id))

    def find_sessions(self, metadata):
        # Auto-pagination is essential: the first page cannot prove absence.
        return [as_dict(session) for session in self.sessions.list(order="desc", limit=100)
                if all(session.metadata.get(k) == v for k, v in metadata.items())]

    def items(self, session_id):
        return [as_dict(item) for item in self.sessions.items.list(session_id, order="asc", limit=100)]

    def turns(self, session_id):
        return [as_dict(turn) for turn in self.sessions.turns.list(session_id, order="asc", limit=100)]

    def send_events(self, session_id, events, key):
        self.sessions.events.create(session_id, events=events, idempotency_key=key)

    def artifacts(self, session_id):
        return [as_dict(item) for item in self.sessions.artifacts.list(session_id)]

    def artifact_content(self, session_id, artifact_id):
        return self.sessions.artifacts.content(artifact_id, session_id=session_id).content

    def cancel(self, session_id, key):
        self.send_events(session_id, [{"type": "agent.session.input.cancel"}], key)

    def delete(self, session_id):
        return as_dict(self.sessions.delete(session_id))
