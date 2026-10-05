"""Configuration for the resolution ladder. Environment only, no config file.

BRIDGE_MODEL_API   anthropic (default) | claude-cli | none
BRIDGE_MODEL       model id for the Anthropic Messages API (default claude-sonnet-5)
BRIDGE_FAST_MODEL  a fast model for the quick calls (area mapping, briefs, kickoff triage;
                   default claude-haiku-4-5-20251001)
ANTHROPIC_API_KEY  read from the environment at call time, never stored
BRIDGE_SEMANTIC    0 turns the model-backed rungs off; default on when a backend exists
BRIDGE_DEEP        0 keeps the fast model points only (area mapping, briefs, kickoff triage, record
                   confirmation) and skips query expansion, near-twin checks and follow-up rewriting
BRIDGE_LIVE        1 turns on live git lookups at ask time; default off
BRIDGE_CONFORMANCE_MODEL  main (default) or fast: which model reads the diff against each signed
                   answer at the finish
BRIDGE_CLAUDE_BIN  path to the claude CLI when it is not on PATH
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    model_api: str = "anthropic"  # anthropic | claude-cli | none
    model: str = "claude-sonnet-5"
    fast_model: str = "claude-haiku-4-5-20251001"
    user_name: str = ""

    def fast(self) -> "Config":
        """The same configuration with the fast model selected, for the
        quick calls that run on every question."""
        from dataclasses import replace
        return replace(self, model=self.fast_model)

    @property
    def api_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def has_backend(self) -> bool:
        """A model backend exists: an API key, or the claude CLI when the
        API is set to it or as the automatic fallback."""
        if self.model_api == "none":
            return False
        if self.model_api == "claude-cli":
            from .llm import find_claude
            return find_claude() is not None
        if self.api_key:
            return True
        from .llm import find_claude
        return find_claude() is not None

    @property
    def semantic_retrieval(self) -> bool:
        """The model-backed rungs (selector, composer, expansion, follow-up
        context, precedent judgment). BRIDGE_SEMANTIC=0 turns them off; with
        no backend at all they are off and the ladder runs its deterministic
        rungs only."""
        flag = os.environ.get("BRIDGE_SEMANTIC", "")
        if flag == "0":
            return False
        if flag == "1":
            return True
        return self.has_backend()

    @property
    def deep_retrieval(self) -> bool:
        """The slow model rungs on top of the semantic ones: query
        expansion with a wide selection, the same-decision check on
        near-twins, follow-up rewriting. BRIDGE_DEEP=0 keeps the fast
        points only (area mapping, briefs, kickoff triage, the selector
        confirming a record) so a node comes back in seconds."""
        return self.semantic_retrieval and os.environ.get("BRIDGE_DEEP", "1") != "0"

    @property
    def live_retrieval(self) -> bool:
        """Live git lookups at ask time (git log --grep on the ingested
        repo). Off by default: the ladder answers from what was ingested."""
        return os.environ.get("BRIDGE_LIVE", "0") == "1"


def load() -> Config:
    cfg = Config()
    api = os.environ.get("BRIDGE_MODEL_API", "").strip().lower()
    if api in ("anthropic", "claude-cli", "none"):
        cfg.model_api = api
    model = os.environ.get("BRIDGE_MODEL", "").strip()
    if model:
        cfg.model = model
    fast = os.environ.get("BRIDGE_FAST_MODEL", "").strip()
    if fast:
        cfg.fast_model = fast
    cfg.user_name = os.environ.get("BRIDGE_USER_NAME", "").strip()
    return cfg
