"""The question a replayed commit was deciding, and its pathless twin.

Template first: "area: Do the thing" becomes "Should area do the thing?".
With --questions model, one cached model call per item rewrites the
subject and body into the same shape for the subjects the template
handles badly (bare nouns, version bumps). The PATHLESS variant strips
every file path, filename, and directory name known to the repository
tree at the replay revision, which is what an enterprise question looks
like: almost nobody names a file."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

_AREA_RE = re.compile(r"^([A-Za-z0-9_./,+*-]+(?:\s*,\s*[A-Za-z0-9_./+*-]+)*)\s*:\s+(.+)$")
_EXT_RE = re.compile(r"^[\w.+-]+\.(?:c|h|cc|cpp|hpp|py|rs|js|mjs|cjs|ts|md|rst|txt|json|ya?ml|toml|go|sh|nix"
                     r"|dts|dtsi|inc|S|in|am|mak|build|cfg|conf|html|css|svg|png|tex|pl|perl|sql|gyp|gypi)$", re.I)
_EDGE_RE = re.compile(r"^[\"'`(\[{<]+|[\"'`)\]}>,.;:!?]+$")


def split_subject(subject: str) -> tuple[str, str]:
    m = _AREA_RE.match(subject.strip())
    if m and not m.group(1).lower().startswith(("revert", "fixup", "squash", "merge")):
        return m.group(1).strip(), m.group(2).strip()
    return "", subject.strip()


def _lower_first(text: str) -> str:
    if not text:
        return text
    first = text.split()[0]
    if first.isupper() and len(first) > 1:
        return text
    return text[0].lower() + text[1:]


def template_question(subject: str) -> str:
    area, rest = split_subject(subject)
    rest = rest.rstrip(".").strip()
    rest = re.sub(r"\s*\(#\d+\)\s*$", "", rest)
    if not rest:
        rest = subject.strip()
    body = _lower_first(rest)
    if area:
        return f"Should {area} {body}?"
    return f"Should we {body}?"


def routing_form(decision: str) -> str:
    """The accountability question the bench asks Raven: who should
    approve this decision."""
    return f"Who should approve this change: {decision}"


def context_from_body(body: str, limit: int = 600) -> str:
    text = " ".join((body or "").split())
    return text[:limit]


# ---------------- pathless stripping ----------------

class TreeTokens:
    """Path components and basename stems of the tree at a revision."""

    def __init__(self, repo: str, rev: str, scope: list[str] | None = None):
        self.tokens: set[str] = set()
        args = ["git", "-C", repo, "ls-tree", "-r", "--name-only", rev]
        if scope:
            args += scope
        out = subprocess.run(args, capture_output=True, text=True, timeout=300)
        for line in out.stdout.splitlines():
            self._add_path(line.strip())
        top = subprocess.run(["git", "-C", repo, "ls-tree", "--name-only", rev], capture_output=True,
                             text=True, timeout=60)
        for line in top.stdout.splitlines():
            self._add_path(line.strip())

    def _add_path(self, path: str) -> None:
        if not path:
            return
        for comp in path.split("/"):
            c = comp.lower()
            if not c:
                continue
            self.tokens.add(c)
            stem = c.rsplit(".", 1)[0] if "." in c else c
            if len(stem) >= 2:
                self.tokens.add(stem)

    def strip(self, text: str) -> str:
        out: list[str] = []
        for word in (text or "").split():
            core = _EDGE_RE.sub("", word)
            low = core.lower()
            if not core:
                continue
            if "/" in core and not core.startswith(("http", "https")):
                continue
            if _EXT_RE.match(core):
                continue
            if low in self.tokens and len(low) >= 2:
                continue
            if low.rstrip(":") in self.tokens:
                continue
            out.append(word)
        text = " ".join(out)
        text = re.sub(r"\s+([,.;:?!])", r"\1", text)
        text = re.sub(r"^(Should|Who should approve this change:)\s*:?\s*[,:]?\s*", r"\1 ", text)
        text = re.sub(r"\bShould\s+(do|add|fix|make|use|drop|remove|move|allow)\b", r"Should we \1", text)
        text = re.sub(r"\bShould\s*\?", "Should we change this?", text)
        return " ".join(text.split())


# ---------------- optional model rephrase, cached ----------------

REPHRASE_SYSTEM = (
    "You turn one git commit (subject and message) into the decision it was making, as a single "
    "question of the form 'Should X do Y?'. X is the subsystem or component the commit names, kept "
    "verbatim from the subject prefix when there is one (for example 'hw/riscv' or 'drm/amd/display' or "
    "'util'). Y is the concrete change. One sentence, at most 28 words, no code, no dashes of any kind, "
    "no preamble. Return ONLY the question."
)


def rephrase_with_model(items: list, cache_path: Path, model: str, log=print) -> dict[str, str]:
    """One model call per item (cached by sha) that writes the decision
    question; falls back to the template on any error."""
    from bridge.config import Config
    from bridge.llm import Client, LLMError
    cache: dict[str, str] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
    cfg = Config(model=model)
    client = Client(cfg)
    misses = [it for it in items if it.sha not in cache]
    if misses:
        log(f"rephrasing {len(misses)} questions with {model} (cached: {len(cache)})")
    for n, it in enumerate(misses, 1):
        prompt = f"Subject: {it.subject}\n\nMessage:\n{it.body[:1200]}"
        try:
            text = client.complete("rephrase", REPHRASE_SYSTEM, prompt, max_tokens=120).strip()
            text = text.splitlines()[0].strip().strip('"')
            if not text.lower().startswith("should") or len(text) > 240:
                text = template_question(it.subject)
        except LLMError as e:
            log(f"  rephrase failed for {it.sha[:10]}: {e}")
            text = template_question(it.subject)
        cache[it.sha] = text
        if n % 25 == 0:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    return {it.sha: cache.get(it.sha) or template_question(it.subject) for it in items}
