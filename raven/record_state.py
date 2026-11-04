"""Source lifecycle is evidence, never an authority or approval signal."""
import re


SETTLED_STATES = frozenset({"closed", "done", "resolved", "complete", "completed",
                            "merged", "accepted", "approved", "published", "effective", "final"})


def resolved_value(value):
    """Parse the same explicit boolean spellings on REST and MCP."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "1", "yes"):
            return True
        if word in ("false", "0", "no"):
            return False
    raise ValueError("resolved must be a boolean or true/false, yes/no, 1/0")


def state_settled(status):
    """Unknown/nonterminal states remain visible but cannot settle a question.

    Empty status preserves the legacy unqualified-record behavior. This only
    controls evidence eligibility; even a terminal source cannot approve work.
    """
    state = re.sub(r"[\s_-]+", " ", str(status or "").strip().lower())
    return not state or state in SETTLED_STATES
