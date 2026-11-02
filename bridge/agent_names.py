"""The names coding agents go by on screen.

A host hook registers a task under its own identifier ("claude", "codex"),
and the task card then read "claude · for Alex Moreno". People know the
products by name, so one table here maps the identifiers Raven sees to
Claude Code, Codex, Cursor and the rest; anything unknown is shown as it
came in, and nothing at all reads "Coding agent".
"""
from __future__ import annotations

DEFAULT = "Coding agent"

_KNOWN = {
    "claude": "Claude Code", "claude-code": "Claude Code", "claude_code": "Claude Code",
    "claudecode": "Claude Code", "claude code": "Claude Code", "claude-cli": "Claude Code",
    "codex": "Codex", "codex-cli": "Codex", "openai-codex": "Codex", "openai codex": "Codex",
    "cursor": "Cursor", "cursor-agent": "Cursor",
    "opencode": "OpenCode", "open-code": "OpenCode",
    "pi": "Pi",
    "copilot": "GitHub Copilot", "github-copilot": "GitHub Copilot",
    "gemini": "Gemini CLI", "gemini-cli": "Gemini CLI",
}


def display_name(raw) -> str:
    """The product name for an agent identifier: 'claude' is Claude Code,
    'codex' is Codex, 'cursor' is Cursor. A name Raven does not know is
    returned as it was given; nothing becomes 'Coding agent'."""
    text = " ".join(str(raw or "").split())
    if not text:
        return DEFAULT
    return _KNOWN.get(text.lower(), text)
