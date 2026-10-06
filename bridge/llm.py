"""One model client for the ladder, standard library only.

Every completion is real inference: the Anthropic Messages API on the
user's own key (read from ANTHROPIC_API_KEY at call time, never written
anywhere), or the claude CLI (claude -p, on the user's plan) when
BRIDGE_MODEL_API=claude-cli or as the automatic fallback when no key is
set. With neither, completion raises NoAPIKey and the ladder runs its
deterministic rungs only; nothing is faked.

Embeddings are ALWAYS local and deterministic (hashed bag of stemmed
words). No embedding API is ever called.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass

from .config import Config

EMBED_DIM = 256

_STEM_SUFFIXES = ("ing", "ed", "es", "s")


def clip_marked(text: str, limit: int, rest: str) -> str:
    """The text whole when it fits; else cut at a word before `limit` and
    marked, never mid-word and never silently."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{cut} … [cut: {rest}]"


def stem(token: str) -> str:
    """Light suffix stripping so paged/paging/pages all read as one term."""
    for suf in _STEM_SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 3:
            return token[: -len(suf)]
    return token


def embed(text: str) -> list[float]:
    """Deterministic local embedding: hashed bag of stemmed words,
    L2-normalized."""
    vec = [0.0] * EMBED_DIM
    for raw in re.findall(r"[a-z0-9]+", text.lower()):
        token = stem(raw)
        h = hashlib.sha1(token.encode()).digest()
        idx = int.from_bytes(h[:4], "big") % EMBED_DIM
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = sum(x * x for x in vec) ** 0.5
    if norm == 0:
        return vec
    return [x / norm for x in vec]


JSON_MAX_TOKENS = 8000

# Advisory reads keep the existing 1200-token visible-answer target. Sparse
# findings fit that target; thinking has its separate existing allowance.
# These are output/transport limits, not a quota on requirements to examine.
REVIEW_MAX_TOKENS = 1200
REVIEW_MAX_CHARS = 6000
# A separate input ceiling for the complete authority context, not an output
# allowance or a token conversion. Oversized authority is retained but not read.
REVIEW_CONTEXT_MAX_CHARS = 6000
REVIEW_MAX_FINDINGS = 4
REVIEW_CONTRACT_VERSION = "approved-source-v3"


def _review_effort(cfg, purpose, bounded):
    """Only the documented exact model/backend has an advisory effort policy."""
    if (bounded and cfg.model_api == 'anthropic' and cfg.model == 'claude-sonnet-5'
            and cfg.api_key and purpose in ('conformance', 'counterexample', 'conditions')):
        return 'medium'
    return None


class LLMError(RuntimeError):
    pass


class NoAPIKey(LLMError):
    pass


def _anthropic_text(payload: dict) -> str:
    """Pull the assistant's text out of a /v1/messages response. Never
    index content[0] blindly: thinking-capable models put a thinking block
    first."""
    if payload.get("type") == "error":
        err = payload.get("error") or {}
        raise LLMError(f"model API error: {err.get('type', 'unknown')}: "
                       f"{err.get('message', '')}".strip())
    blocks = payload.get("content") or []
    text = "".join(b.get("text", "") for b in blocks
                   if isinstance(b, dict) and b.get("type") == "text")
    if text.strip():
        return text
    if payload.get("stop_reason") == "max_tokens":
        raise LLMError("the model hit max_tokens before producing any text")
    kinds = ", ".join(sorted({b.get("type", "?") for b in blocks
                              if isinstance(b, dict)})) or "none"
    raise LLMError(f"the model returned no text block (blocks: {kinds})")


# Room for a model's thinking on top of the tokens a call asks for. The
# thinking is not returned, it is only budgeted: a model that thinks
# longer than this still answers, cut short, and complete_json retries.
# Measured on the conformance read of a 4700-character diff: 2000 to 4500
# tokens of thinking.
THINKING_ROOM = 8000
_THINKS: set[str] = set()


def _cut_short_by_thinking(payload: dict) -> bool:
    """The model thought, then ran out of tokens: its answer is missing
    or truncated."""
    return (payload.get("stop_reason") == "max_tokens"
            and any(isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                    for b in payload.get("content") or []))


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, payload: dict) -> None:
        u = payload.get("usage") or {}
        self.calls += 1
        self.input_tokens += int(u.get("input_tokens") or 0)
        self.output_tokens += int(u.get("output_tokens") or 0)

    def summary(self) -> str:
        if not self.calls:
            return ""
        return (f"{self.calls} model calls, "
                f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens")


USAGE = Usage()


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post_json(self, url: str, headers: dict, body: dict,
                   timeout: int = 120) -> dict:
        """POST with backoff on the retryable statuses; every HTTP error
        surfaces as LLMError. Standard library only."""
        import time as _time
        import urllib.error
        import urllib.request
        data = json.dumps(body).encode()
        last = None
        for attempt in range(4):
            req = urllib.request.Request(
                url, data=data,
                headers={**headers, "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    payload = json.loads(e.read().decode())
                    err = payload.get("error") if isinstance(payload, dict) else None
                    detail = (str(err.get("message") or err.get("type"))
                              if isinstance(err, dict) else str(payload))[:300]
                except Exception:
                    detail = "no detail"
                if e.code not in (408, 429, 500, 502, 503, 529):
                    raise LLMError(f"model API error {e.code}: {detail}")
                last = f"model API error {e.code}: {detail}"
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = f"could not reach {url}: {type(e).__name__}"
            if attempt < 3:
                _time.sleep(float(2 ** attempt))
        raise LLMError(f"{last}. Tried 4 times; the model API is refusing "
                       f"requests right now.")

    def complete(self, purpose: str, system: str, prompt: str,
                 max_tokens: int = 2000, *, bounded: bool = False) -> str:
        if self.cfg.model_api == "none":
            raise NoAPIKey("model backend disabled (BRIDGE_MODEL_API=none)")
        if self.cfg.model_api == "claude-cli":
            USAGE.calls += 1
            return _claude_cli(system, prompt, self.cfg.model)
        if not self.cfg.api_key:
            if find_claude():
                USAGE.calls += 1
                return _claude_cli(system, prompt, self.cfg.model)
            raise NoAPIKey(
                "no ANTHROPIC_API_KEY in the environment and no claude CLI "
                "found; the ladder runs its deterministic rungs only")
        # Sonnet 5 defaults to adaptive/high. Its manual thinking budget is
        # unsupported, so advisory reads use the documented medium effort
        # signal instead. This is NOT a hard reserve for response text. Keep
        # unverified model IDs and all non-review requests exactly as before.
        # https://platform.claude.com/docs/en/build-with-claude/effort
        effort = _review_effort(self.cfg, purpose, bounded)
        review_options = {'effort': effort} if effort is not None else {}
        thinks = self.cfg.model in _THINKS
        payload = self._messages(system, prompt, max_tokens + (THINKING_ROOM if thinks else 0), **review_options)
        if not thinks and _cut_short_by_thinking(payload):
            # This model thinks before it answers, and the thinking counts
            # against max_tokens. Measured live on the conformance read: a
            # 1200-token budget went to thinking on two of six decisions,
            # no text came back, and the reads were silently missing. Every
            # call to it from now on gets room to think; this one is asked
            # again with that room.
            _THINKS.add(self.cfg.model)
            thinks = True
            payload = self._messages(system, prompt, max_tokens + THINKING_ROOM, **review_options)
        if payload.get("stop_reason") == "max_tokens":
            if bounded:
                # An advisory reading is allowed to be inconclusive. Repeating
                # the same search with ever more room can consume minutes and
                # still yield no usable evidence. Keep the one-time thinking
                # discovery above, but do not double this stage's budget.
                raise LLMError(f"the model's {purpose} reading exhausted its budget; inconclusive")
            # The answer itself ran out of room. It is asked once more with
            # twice the room, and a text still cut short is never returned as
            # if it were whole: a caller would store it as the answer.
            payload = self._messages(system, prompt, 2 * max_tokens + (THINKING_ROOM if thinks else 0), **review_options)
            if payload.get("stop_reason") == "max_tokens":
                raise LLMError(f"the model's {purpose} answer was cut off at max_tokens twice; not used")
        return _anthropic_text(payload)

    def _messages(self, system: str, prompt: str, max_tokens: int, *, effort: str | None = None) -> dict:
        body = {"model": self.cfg.model, "max_tokens": max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        if effort is not None:
            body['output_config'] = {'effort': effort}
        payload = self._post_json(
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01"}, body)
        USAGE.add(payload)
        return payload

    def complete_json(self, purpose: str, system: str, prompt: str,
                      max_tokens: int = JSON_MAX_TOKENS, *, bounded: bool = False) -> object:
        text = self.complete(purpose, system, prompt, max_tokens=max_tokens,
                             **({'bounded': True} if bounded else {}))
        try:
            return _extract_json(text)
        except json.JSONDecodeError as error:
            if bounded:
                raise LLMError(f"the model's {purpose} reading returned incomplete JSON; inconclusive") from error
            text = self.complete(
                purpose, system,
                prompt + "\n\nReturn ONLY the raw JSON, no prose, no fences.",
                max_tokens=max_tokens * 2)
            try:
                return _extract_json(text)
            except json.JSONDecodeError as e:
                raise LLMError(
                    f"the model did not return usable JSON for {purpose} "
                    f"after a retry ({e})") from e


_NEUTRAL_CWD: str | None = None

META_LEAK_MARKERS = ("ask_bridge", "CLAUDE.md", "MCP", "this session",
                     "I can't", "I cannot", "I tried", "I checked",
                     "I want to flag", "tool search", "let me get this",
                     "wait, let me", "actually, no", "scratch that")


def looks_meta(text: str) -> bool:
    return any(m in text for m in META_LEAK_MARKERS)


def find_claude() -> str | None:
    """Locate the claude CLI binary. BRIDGE_CLAUDE_BIN overrides; then PATH;
    then the install locations that shell aliases hide."""
    import os
    import shutil
    override = os.environ.get("BRIDGE_CLAUDE_BIN")
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return None
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for cand in (os.path.join(home, ".claude", "local", "claude"),
                 os.path.join(home, ".local", "bin", "claude"),
                 "/opt/homebrew/bin/claude",
                 "/usr/local/bin/claude",
                 os.path.join(home, ".npm-global", "bin", "claude")):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _claude_cli(system: str, prompt: str, model: str = "") -> str:
    """Run the prompt through headless Claude Code (claude -p) from a
    neutral empty directory so no project instructions leak in."""
    import subprocess
    import tempfile
    claude_bin = find_claude()
    if not claude_bin:
        raise LLMError("the claude CLI was not found; set BRIDGE_CLAUDE_BIN "
                       "or use an API key")
    global _NEUTRAL_CWD
    if _NEUTRAL_CWD is None:
        _NEUTRAL_CWD = tempfile.mkdtemp(prefix="bridge-llm-")
    try:
        out = subprocess.run(
            [claude_bin, "-p", "--output-format", "text", "--tools", "",
             "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
             "--disable-slash-commands", "--no-session-persistence",
             *(["--model", model] if model else []), "--system-prompt", system],
            input=prompt, capture_output=True, text=True, timeout=600, cwd=_NEUTRAL_CWD,
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError("claude -p timed out after 600s") from e
    if out.returncode != 0:
        detail = (out.stderr.strip() or out.stdout.strip())[:300]
        raise LLMError(f"claude -p failed (exit {out.returncode}): "
                       f"{detail or 'no output'}")
    return out.stdout


def _extract_json(text: str) -> object:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start = min([i for i in (text.find("["), text.find("{")) if i >= 0], default=0)
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        m = re.search(r"(\[[\s\S]*\])|(\{[\s\S]*\})", text)
        if m:
            return json.loads(m.group(0))
        raise


# ---------------- system prompts (ported verbatim from the ladder) ----------------

BRIEF_SYSTEM = (
    "You write the short brief a busy owner reads before deciding. You are given the decision a coding agent "
    "needs, the context the agent gave, the task it is part of, and the options if any. Write two or three plain "
    "sentences: what is being decided and why it came up, and what the owner is asked to choose. Use only what "
    "you are given. Do not add a fact, a standard, a best practice, a recommendation or a risk the material does "
    "not state in those terms, and where the context is unsure, stay unsure. Where the material says what a "
    "choice leads to, keep each outcome with the choice the material ties it to, in the material's own words; "
    "never move an outcome from one choice to another, and leave an outcome out rather than restate it loosely. "
    "Never say who approves, how approval "
    "works, what Raven checked or found, or who owns the area: the message states those itself. Never say that "
    "no context, information or details were given or are missing: the message shows what Raven found beside "
    "the brief, and a brief that says nothing was provided contradicts it. No preamble, no "
    "headings, no bullet points, never address the owner by name. Return ONLY JSON: {\"brief\": \"...\"}."
)

# A brief's remark that the material was thin: "No context was provided
# about ...", "but no context has been provided about current policies".
# It says nothing about the decision, and next to what Raven found it is
# false. Measured live on 63eb671: thirty briefs said so, one beside the
# two conflicting policies the same message quoted.
_ABSENCE_RE = re.compile(
    r"\bno (?:additional |further |other |specific |background |relevant )?(?:context|information|details?|"
    r"background|explanation|rationale)\b[^.;]*?\b(?:was|were|has been|have been|is|are)\s+"
    r"(?:provided|given|supplied|available|included|shared|offered|specified)\b[^.;]*"
    r"|\b(?:the |this )?(?:context|information|material|details?)(?: (?:provided|given|supplied))? "
    r"(?:does|do) not (?:specify|say|state|mention|explain|include|describe|give|provide|indicate|identify|"
    r"list)\b[^.;]*"
    r"|\b(?:the |this )?(?:material|context|information|agent|request)\s+(?:gives|provides|offers|includes|"
    r"contains|says) no (?:further |additional |other |more )?(?:detail|details|context|information|explanation|"
    r"background|reason)\b[^.;]*", re.IGNORECASE)


def drop_absence(text: str) -> str:
    """The brief without its remarks that no context was given: a
    sentence that is only that is dropped, and a clause joined to what is
    being decided ("..., but no context was provided about ...") is cut
    from it."""
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+", (text or "").strip()):
        m = _ABSENCE_RE.search(sentence)
        if not m:
            kept.append(sentence)
            continue
        head = sentence[:m.start()].rstrip()
        head = re.sub(r"[,;:]?\s*(?:but|and|though|although|yet|however)?\s*$", "", head, flags=re.IGNORECASE).rstrip(" ,;:")
        if len(head.split()) >= 4:
            kept.append(head + ".")
    return " ".join(kept).strip()

BRIEF_CHECK_SYSTEM = (
    "You check a short brief written for a busy owner against the material it was written from: a decision, the "
    "context a coding agent gave, its options, and the task it came up in when given. Find every statement in the brief about what a choice, option "
    "or approach leads to: what it causes, allows, prevents, breaks, costs or keeps. Find also every statement "
    "of fact it makes about the code as it is, an earlier decision or policy, or what the agent found: what "
    "something does today, what a policy keeps or allows, which part a rule applies to. For each, compare it "
    "with the material. \"same\": the material says that outcome follows from that same choice, or says that "
    "fact about that same thing. \"swapped\": the material ties that outcome to a different choice, ties this "
    "choice to a different outcome, or says that fact about a different thing (the material keeps parsing "
    "deterministic and the brief says the sleep logic stays deterministic). \"unsupported\": the material "
    "does not say it. Read closely: \"after the clamp\" and \"before the clamp\" are different choices, and so "
    "are \"clamp after jitter\" and \"jitter after clamp\". A brief that states no outcome and no fact has no "
    "claims. Return ONLY JSON: {\"claims\": [{\"claim\": \"...\", \"verdict\": \"same\" or \"swapped\" or "
    "\"unsupported\"}]}. No em dashes or en dashes."
)

_BRIEF_STOP = {"the", "and", "for", "that", "this", "with", "then", "than", "from", "into", "onto", "its", "it's",
               "are", "was", "were", "been", "being", "can", "could", "may", "might", "will", "would", "should",
               "which", "what", "when", "where", "while", "who", "all", "any", "each", "only", "also", "not", "but",
               "though", "about", "exactly", "case", "again", "there", "their", "them", "they", "is", "be", "to",
               "of", "in", "on", "at", "by", "or", "if", "a", "an", "as", "so", "up", "do", "does", "did", "has",
               "have", "had", "one", "some", "more", "most", "just", "even", "own", "same", "other"}
_BRIEF_IF_RE = re.compile(r"\bif\s+([^,.;:]{3,160}?),\s*(?:then\s+)?([^.;]{3,400})", re.IGNORECASE)
_BRIEF_CLAUSE_RE = re.compile(r"[.!?;]\s+|,\s*(?:while|whereas|but|and)\s+|\s+(?:while|whereas)\s+", re.IGNORECASE)


def _brief_tokens(text: str) -> set[str]:
    """Content words, stemmed; an identifier or a hyphenated word
    (Retry-After, thundering-herd) stays one word."""
    return {stem(w) for w in re.findall(r"[a-z0-9_]+(?:[-'][a-z0-9_]+)*", (text or "").lower())
            if len(w) > 2 and w not in _BRIEF_STOP}


def _outcome_pairs(context: str, options) -> list[list[tuple[str, str]]]:
    """The (choice, outcome) pairs the agent wrote, in groups that can be
    told apart: "If <choice>, <outcome>" sentences in its context, and
    options written "<choice>: <outcome>"."""
    ifs = [(m.group(1), m.group(2)) for m in _BRIEF_IF_RE.finditer(context or "")]
    opts = []
    for option in options or []:
        label, sep, outcome = str(option).partition(":")
        if sep and label.strip() and outcome.strip():
            opts.append((label.strip(), outcome.strip()))
    return [group for group in (ifs, opts) if len(group) >= 2]


def swapped_outcome(brief: str, context: str, options=None) -> str:
    """A clause of the brief that ties one of the agent's choices to the
    outcome the agent gave for another, or empty. Deterministic and
    conservative: it names a swap only when the clause names one choice
    by words no other choice uses and carries words only another choice's
    outcome uses. Measured live on a397f1c: the agent wrote "if jitter is
    added after the clamp, a wait can exceed retry_after_max ... if before,
    jitter collapses to zero near the cap", and the brief said the
    opposite of both."""
    for group in _outcome_pairs(context, options):
        choice_words = [_brief_tokens(c) for c, _ in group]
        outcome_words = [_brief_tokens(o) for _, o in group]
        every_choice = set().union(*choice_words)
        own_choice = [w - set().union(*(x for j, x in enumerate(choice_words) if j != i))
                      for i, w in enumerate(choice_words)]
        own_outcome = [w - every_choice - set().union(*(x for j, x in enumerate(outcome_words) if j != i))
                       for i, w in enumerate(outcome_words)]
        phrases = [" ".join(c.lower().split()) for c, _ in group]
        for clause in _BRIEF_CLAUSE_RE.split(brief or ""):
            words = _brief_tokens(clause)
            flat = " ".join(clause.lower().split())
            named = [i for i, p in enumerate(phrases) if p and p in flat]
            if len(named) != 1:
                named = [i for i, w in enumerate(own_choice) if w and w & words]
            if len(named) != 1:
                continue
            i = named[0]
            if own_outcome[i] & words:
                continue
            for j, w in enumerate(own_outcome):
                if j != i and w and len(w & words) >= min(2, len(w)):
                    return (f"the brief ties \"{group[i][0].strip()}\" to what the agent said follows from "
                            f"\"{group[j][0].strip()}\": \"{clause.strip()[:200]}\"")
    return ""


def check_brief(cfg, question: str, context: str, options, brief: str, task: str = "") -> list[dict] | None:
    """Each outcome the brief states, checked against the agent's own
    words by a second, separate reading. None when the check could not be
    made, which withholds the brief. The task the brief was written with
    is material too: measured on the 63eb671 questions, a brief that said
    which task the choice came up in was withheld as unsupported."""
    prompt = ((f"TASK: {task}\n" if task else "")
              + f"DECISION: {question}\nCONTEXT: {(context or '').strip()[:3000] or 'none given'}\n"
              f"OPTIONS: {' | '.join(options) if options else 'open'}\n\nBRIEF: {brief}")
    try:
        raw = Client(cfg.fast()).complete_json("brief_check", BRIEF_CHECK_SYSTEM, prompt, max_tokens=800)
    except LLMError:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("claims"), list):
        return None
    return [c for c in raw["claims"] if isinstance(c, dict)]

TRIAGE_SYSTEM = (
    "You decide whether a coding agent's task needs a person's judgment before or during the work, or can proceed "
    "on its own. You are given the task as kicked off, what Raven found (the areas it touches, the people who own "
    "them, listings, prior decisions, pending questions) and the rule-based verdict with its reason. Engage only "
    "for a reason: a real tradeoff the owners would want to make themselves (user-facing behaviour, compatibility, "
    "data, security, cost, policy, anything that reverses or changes what people decided), or the task itself asks "
    "a question. Pass on routine work with a clear owner: refactors, fixes, tests, docs, additive changes inside one "
    "area, even large ones. Most tasks are pass. Return ONLY JSON: {\"verdict\": \"engage\" or \"pass\", \"why\": "
    "one sentence naming the reason and the person if any}."
)


def compose_brief(cfg, question: str, context: str, task_title: str, evidence: list[str] | None = None,
                  options: list[str] | None = None, why: list | None = None) -> str:
    """Two or three sentences for the owner, from the decision, its
    context, the task and the options only. Who owns it, why them, and
    how it gets approved are facts the message states itself; given to
    the model, they came back wrong. Measured live: routing notes became
    "approval comes from git trailers in the commit" in a brief for a
    decision that needed the person it was sent to, and a context that
    said no standard exists became "the clear standard". `evidence` is
    accepted and not used. Empty when no backend answers.

    The brief is shown only once it is checked against the agent's own
    words, twice: a deterministic reading for a choice tied to another
    choice's outcome, and a separate model reading of every outcome it
    states. Either finding a swapped or unsupported outcome, or the check
    failing, withholds it, and the owner reads the agent's words instead.
    Measured live on a397f1c: the brief reversed which choice exceeds the
    cap and which collapses jitter at it. `why` collects the reason."""
    client = Client(cfg.fast())
    options = options or []
    prompt = (f"TASK: {task_title}\nDECISION: {question}\nCONTEXT: {(context or '').strip()[:3000] or 'none given'}\n"
              f"OPTIONS: {' | '.join(options) if options else 'open'}")
    try:
        raw = client.complete_json("brief", BRIEF_SYSTEM, prompt, max_tokens=400)
    except LLMError:
        return ""
    text = str(raw.get("brief", "")).strip() if isinstance(raw, dict) else ""
    # Whole or none: a brief cut mid-sentence reads as if it were whole.
    if not text or looks_meta(text) or len(text) > 1200:
        return ""
    text = drop_absence(text)
    if not text:
        return ""
    swap = swapped_outcome(text, context, options)
    if swap:
        if why is not None:
            why.append(swap)
        return ""
    claims = check_brief(cfg, question, context, options, text, task_title)
    if claims is None:
        if why is not None:
            why.append("the brief could not be checked against the agent's words")
        return ""
    wrong = [c for c in claims if str(c.get("verdict", "")).strip().lower() in ("swapped", "unsupported")]
    if wrong:
        if why is not None:
            why.append("; ".join(f"{str(c.get('verdict')).lower()}: {str(c.get('claim', '')).strip()[:200]}"
                                 for c in wrong[:3]))
        return ""
    return text


PROPOSAL_SYSTEM = (
    "You are Raven's proposal writer. You are given one open decision and a few answers ONE person "
    "gave before on related decisions, with their rationale. Write, in two sentences at most, how that "
    "person would most likely decide this one, grounded only in those answers: cite the pattern you see. "
    "The proposal has to answer the decision as it is asked: when it asks for a value, a default, a name or "
    "a choice, the proposal states one. If the earlier answers say nothing that answers it, however related "
    "they look, set answers to false: an answer about documentation is no answer to a question about a "
    "default value. Describe one mechanism and one value; never two that contradict each other. An earlier "
    "answer may carry a scope that is not met here (another customer, another release, other files, or an "
    "expiry that passed); lean on it only as an analogy. Say how the proposal relates to the earlier "
    "answers: \"same_policy\" when one of them states a general rule that covers this decision as written, "
    "\"analogy\" when you infer from answers about other things. Return JSON: {\"proposal\": \"...\", "
    "\"answers\": true|false, \"relation\": \"same_policy\" or \"analogy\"}.")


def compose_proposal(cfg, owner: str, question: str, context: str, priors: list[dict]) -> tuple[str, str]:
    """How this owner would likely decide, from their own signed answers
    only, as (proposal, relation); a prediction for them to confirm, never
    sign-off. Empty when the backend does not answer, or when what it
    wrote does not answer the decision as asked. Measured live on eb9d22d:
    a question about a default value got a proposal about documentation."""
    client = Client(cfg.fast())
    shown = "\n".join(f"- Q: {p['question'][:200]}\n  A: {p['answer'][:300]}"
                       + (f"\n  Because: {p['rationale'][:200]}" if p.get("rationale") else "")
                       + (f"\n  Declared scope, not met here: {p['scope'][:200]}" if p.get("scope") else "")
                       for p in priors[:4])
    prompt = (f"PERSON: {owner}\nDECISION: {question}\nCONTEXT: {(context or '').strip()[:1200] or 'none given'}\n"
              f"THEIR EARLIER ANSWERS:\n{shown}")
    try:
        raw = client.complete_json("proposal", PROPOSAL_SYSTEM, prompt, max_tokens=300)
    except LLMError:
        return "", ""
    # "grounded" was the flag before "answers"; a reply with neither is not one.
    if not isinstance(raw, dict) or not raw.get("answers", raw.get("grounded")):
        return "", ""
    text = str(raw.get("proposal", "")).strip()
    relation = "same_policy" if str(raw.get("relation", "")).strip().lower() == "same_policy" else "analogy"
    return ("", "") if looks_meta(text) or len(text) > 800 else (text, relation)


def model_triage(cfg, title: str, goal: str, discovery: dict, verdict: str, why: str) -> dict:
    """The fast model's engage or pass, advisory, from the same digest the
    rules saw. Empty when no backend answers."""
    client = Client(cfg.fast())
    people = ", ".join(p["name"] for p in (discovery.get("people") or [])[:3]) or "nobody clear"
    areas = ", ".join(a["path"] for a in (discovery.get("areas") or [])[:5]) or "none found"
    listings = "; ".join(f"{e['person']} for {e['pattern']}" for e in (discovery.get("listings") or [])[:5]) or "none"
    prior = "; ".join(f"\"{p['question'][:100]}\" answered by {p.get('answered_by') or 'someone'}"
                      for p in (discovery.get("prior_decisions") or [])[:3]) or "none"
    pending = "; ".join(f"\"{p['question'][:100]}\" waiting on {p.get('owner') or 'nobody'}"
                        for p in (discovery.get("pending") or [])[:3]) or "none"
    prompt = (f"TASK: {title}\nGOAL: {(goal or '').strip()[:1500] or 'not stated'}\nREQUESTER: "
              f"{discovery.get('requester') or 'unknown'}\nAREAS: {areas}\nPEOPLE THE SIGNALS NAME: {people}\n"
              f"LISTINGS: {listings}\nPRIOR DECISIONS: {prior}\nPENDING: {pending}\n"
              f"RULE-BASED VERDICT: {verdict} ({why})")
    try:
        raw = client.complete_json("triage", TRIAGE_SYSTEM, prompt, max_tokens=300)
    except LLMError:
        return {}
    if not isinstance(raw, dict) or raw.get("verdict") not in ("engage", "pass"):
        return {}
    return {"verdict": raw["verdict"], "why": str(raw.get("why", "")).strip()[:500]}


SELECTOR_SYSTEM = (
    "You are Raven's retrieval selector. You are given one question and a "
    "numbered list of candidate items from an org's memory (signed past "
    "answers) and records (merged PRs and tickets), each with its age. "
    "Pick the ONE candidate that directly and currently answers the "
    "question, or none. Rules: the candidate must match THIS question's "
    "actual scope AND conditions: phase, location, tier, unit type, time "
    "window; a rule for production phase does not answer a seed-train "
    "question, a domestic default does not answer an overseas case. A "
    "specific rule matching the question's conditions beats a general "
    "default. When two candidates disagree, the newer one governs, "
    "including a newer change that bypasses or reverts an older process "
    "still described elsewhere. A candidate that only describes a "
    "problem, request, or investigation answers nothing, EXCEPT a "
    "candidate marked [status only]: it answers questions about progress, "
    "completion, or who is tracking or assigned to the work (the listed "
    "author or assignee), and nothing else. When several records amend "
    "the same value in a chain, the newest link governs; intermediate "
    "values are history. BUT a question asking about the ORIGINAL value, "
    "the history of changes, or what a figure was benchmarked against is "
    "answered by the record carrying that history, never by the "
    "current-value memory. A question PROPOSING a change to current "
    "behavior is not answered by items describing that current behavior; "
    "pick none so it routes to the accountable human. A question asking "
    "WHO decided, made, or authored a change is answered by the record of "
    "that change: its author line and its PR or ticket reference are the "
    "answer even when the body does not narrate the decision. A WHY "
    "question needs a candidate that states a motivation; the record that "
    "merely made the change without saying why answers who and when, not "
    "why. If no candidate "
    "truly answers under these rules, say none. You select, you never "
    "write content. Return ONLY JSON: "
    "{\"pick\": \"m2\" or \"r4\" or \"none\", \"why\": one sentence}."
)

SAME_DECISION_SYSTEM = (
    "You judge whether two questions ask for the SAME decision, such that "
    "one answer settles both. Same decision means the same subject and the "
    "same variable being decided; wording may differ freely, and a "
    "question that offers concrete options for the variable the other "
    "question asks about openly IS the same decision ('how should the "
    "route be wired in?' and 'registered via add_url_rule, or through a "
    "Blueprint?' are one decision). Different settings or defaults "
    "(SESSION_COOKIE_SECURE vs SESSION_COOKIE_SAMESITE) or different "
    "artifacts are NOT the same decision. Direction matters for scope: "
    "when the EXISTING question Q2 is broader and answering it would "
    "necessarily settle the new question Q1 (Q2 asks for the key shape "
    "AND its default, Q1 asks only for the default), that IS the same: "
    "one answer to Q2 settles both. Only when Q1 asks something Q2's "
    "answer would not cover is it a different decision. Return ONLY "
    "JSON: {\"same\": true} or {\"same\": false}."
)

CONFLICT_SYSTEM = (
    "You compare an answer already on file against one org record, both "
    "about the same question. Reply with exactly one word. CONFLICT if the "
    "record states something that cannot both be true with the answer: a "
    "different value, a different rule, a different set of who may do it. "
    "AGREE if the record supports the answer or merely adds detail. "
    "UNRELATED if the record does not actually bear on the question. Two "
    "statements can share almost all their vocabulary and still conflict, "
    "so compare what each one CLAIMS, not the words they use. Say nothing "
    "except the single word."
)

COMPOSER_SYSTEM = (
    "You turn one cited org record into a direct answer to a question. Use "
    "ONLY facts stated in the record text you are given. If the record "
    "states the needed fact, answer in one or two sentences with the "
    "concrete values. If it does not fully state it, say plainly what the "
    "record does establish and what it leaves unanswered. Never invent "
    "values, names, dates, or scope beyond the record. A number or "
    "attribution that appears only in the QUESTION and not in the record "
    "text must never be affirmed as a record fact: questioners test with "
    "planted figures, and confirming one launders it into a citation. "
    "Never name a file, "
    "module, or path that does not appear in the record text you were given "
    "or in the question itself: a citation that points at a file the record "
    "never mentioned sends the reader to the wrong module while looking "
    "sourced. When the question "
    "offers specific alternatives (this timestamp or that one, this service "
    "or that one, silently or on request) and the record does not itself "
    "draw that distinction, you MUST NOT pick one: say which part the "
    "record settles and state that it does not choose between the options "
    "asked about. Resolving the exact point in doubt with a detail the "
    "record never contains is the worst thing you can do here, because it "
    "ships as a decision with a real engineer's citation on it. A record "
    "written as "
    "a problem statement plus a change means the CHANGE is what stands "
    "now; state the resulting behavior, not the old problem. For a yes/no "
    "question, never open with a bare Yes or No: open with the outcome "
    "stated in the question's own terms (if the question asks whether "
    "usage is invoiced, the first words are 'That usage is invoiced' or "
    "'Not invoiced'), so the first sentence cannot contradict the rest. "
    "Apply the record's conditions to the question's situation "
    "(dates, phases, windows, amounts): if the record sets a 90 day window "
    "and the question is at day 65, the answer is inside the window; if "
    "the record sets a threshold and the question states an amount, apply "
    "the threshold to that amount. A record that states a rule for a "
    "whole class of things in so many words (every numeric option, all "
    "public endpoints) answers a question about one member of that class "
    "the rule does not exclude: apply it to that member and say it is that "
    "rule applied. Measured live: a signed rule for every numeric Retry "
    "option was read as not answering the question about a new numeric "
    "option, and its owner was asked to confirm their own policy again. "
    "An example the record gives (a sample value, a sample line, a sample "
    "identifier or command) establishes the format or shape it "
    "demonstrates: when the question asks for a format the record shows "
    "by example, answer with that format and quote the example, and never "
    "say the format is not established. Measured live: a ticket gave the "
    "header as an example and the answer said the format was not "
    "established. "
    "The record's "
    "author or assignee is given to you: a question asking who owns, "
    "tracks, or is assigned to the work is answered by that name, and a "
    "question asking who decided, made, or authored the cited change is "
    "answered by naming the author with the record's reference and the "
    "reason the record states. If the "
    "record genuinely does not state the needed fact and states nothing "
    "usefully adjacent either, reply with exactly the sentinel "
    "RECORD DOES NOT ANSWER and nothing else. The record's status, when it "
    "has one, is in brackets after its reference: a record cancelled, "
    "rejected, withdrawn, superseded or obsolete states something that was "
    "NOT adopted, so it never answers what stands now; reply RECORD DOES NOT "
    "ANSWER unless the question asks about that record's own history. Do all reasoning silently: "
    "output ONLY the final answer, never narration, never a "
    "self-correction mid-sentence. Return plain text only, no preamble. "
    "No em dashes or en dashes."
)

# A composed answer, checked against what it was composed from, clause by
# clause. Measured live on 5e967e4: a ticket that capped Retry-After jitter
# at retry_after_max was composed into "for a zero parsed delay the same
# mechanism holds", which the ticket never says.
SUPPORT_SYSTEM = (
    "You check an answer that was written from source text against that text, statement by statement. For each "
    "statement the answer makes, say whether the source states it (\"stated\"), states a rule in so many words "
    "that plainly covers this case (\"applied\"), repeats what is GIVEN (\"given\"), or does not say it and the "
    "answer reached it by reasoning, analogy or assumption (\"inferred\"). GIVEN is what the question, its "
    "context and the task's stated facts say, and the source's own identity: its reference, who wrote or signed "
    "it, when, its status and its stated rationale. When the question says timeout_slack is a numeric option, "
    "the answer may rely on that: it is given. Never question whether the source is real, where it came from or "
    "who wrote it. A statement about a case neither the source nor the given mentions (a zero or empty value, a "
    "second format, another component, a default, a region) is inferred unless the source's own words cover that "
    "case. A format, shape or convention the source demonstrates by example (a sample value, line, identifier or "
    "command) is stated by that example: a statement that follows it is \"applied\", and a statement that the "
    "format is not established contradicts the source. Then write the answer again with only the stated, applied "
    "and given statements, in the same words "
    "where you can: keep the answer's own true statements about what the source does not say, and add none of "
    "your own. For each inferred statement, and only those, write the "
    "question it answered that the source leaves open, one per inferred statement, in the same order. Return ONLY "
    "JSON: {\"claims\": [{\"claim\": \"...\", \"support\": \"stated\" or \"applied\" or \"given\" or "
    "\"inferred\"}], \"answer\": \"the answer with only what is supported, or empty if nothing\", \"open\": "
    "[\"...\"]}. No em dashes or en dashes."
)


# A prediction is checked differently: carrying the person's pattern over to
# a new case is what a prediction is for, and only a concrete detail that
# none of their answers states is invented. Measured live on 63eb671: "Which
# retry metric label and rollout region are approved?" got a prediction to
# use retry_normal_total "in the standard evaluation region", a region no
# answer of theirs names.
PREDICTION_SUPPORT_SYSTEM = (
    "You check a prediction of how one person would decide a question, written from their earlier answers (the "
    "source). Every statement in a prediction carries their pattern to a new case; that is expected. For each "
    "statement, say \"applied\" when it uses only values, names and choices their answers or the GIVEN contain, "
    "even for another customer, component or case; \"given\" when it repeats the question, its context or the "
    "stated facts; and \"inferred\" only when it introduces a concrete value, name, place, region, figure, "
    "identifier or default that appears in neither their answers nor the GIVEN, however natural it sounds. For "
    "example, when their answer says \"Use retry_normal_total\" for normal customers, \"Use retry_normal_total for "
    "enterprise customers\" is applied, and \"in the standard evaluation region\" is inferred if no answer names "
    "that region. Then "
    "write the prediction again without the inferred statements, in the same words where you can, and for each "
    "inferred statement, and only those, the question it answered that their answers leave open, one each, in "
    "the same order. Return ONLY JSON: {\"claims\": [{\"claim\": \"...\", \"support\": \"applied\" or "
    "\"given\" or \"inferred\"}], \"answer\": \"the prediction without inferred statements, or empty\", \"open\": "
    "[\"...\"]}. No em dashes or en dashes."
)


def check_support(cfg, question: str, source: str, answer: str, given: str = "",
                  prediction: bool = False) -> dict | None:
    """{claims, answer, open} for a composed answer, or None when the
    check could not be made. `given` is what the question's context and
    stated facts establish, which the answer may rely on."""
    try:
        raw = Client(cfg.fast()).complete_json(
            "support", PREDICTION_SUPPORT_SYSTEM if prediction else SUPPORT_SYSTEM,
            f"QUESTION: {question}\n\nGIVEN:\n{(given or '').strip()[:2000] or 'nothing beyond the question'}"
            f"\n\nSOURCE:\n{(source or '')[:6000]}\n\nANSWER: {answer}", max_tokens=900)
    except LLMError:
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("claims"), list) else None


ASSUME_COMPOSER_SYSTEM = (
    "You state the safe default assumption for a low-stakes question by "
    "FOLLOWING the cited precedent's pattern. Use only the precedent's "
    "actual pattern: if it shows phased rollouts in batches, the "
    "assumption is a phased rollout in batches; if it shows languages "
    "added routinely to the same infrastructure, the assumption is that "
    "another language follows routinely. Never fall back to a generic "
    "'feature flag, default off' unless the precedent itself used flags. "
    "If the precedent's pattern does not apply to this question at all, "
    "reply with exactly NO PATTERN and nothing else; never narrate that "
    "no assumption can be derived, and never speak in the first person. "
    "One or two sentences, plain text, flagged as an assumption is "
    "handled elsewhere. No em dashes or en dashes."
)

PRECEDENT_SYSTEM = (
    "You judge whether any candidate org record sets a GENUINE precedent "
    "that a low-stakes default can follow for the question. A precedent "
    "counts when its situation is analogous: the same kind of change or "
    "call, even on a sibling service or a different screen of the same "
    "product, and it is settled. It does not count when it merely shares "
    "vocabulary with the question, records an unresolved proposal, or "
    "decided a materially different kind of question. Pick the single "
    "best precedent or none. Return ONLY JSON: {\"pick\": \"r2\"} or "
    "{\"pick\": \"none\"}."
)

JOINT_COMPOSER_SYSTEM = (
    "You turn SEVERAL cited org records into one direct answer to a "
    "question that no single record settles alone. Use ONLY facts stated "
    "in the record texts you are given; you may combine them, including "
    "by one-hop inference a careful engineer would accept: an open "
    "ticket with no shipped change means the work is not done; a "
    "record's author or assignee is who holds it; a rule plus a newer "
    "record narrowing it means the narrowed form stands; a threshold "
    "plus an amount stated in the question means the threshold applies "
    "to that amount. Name the "
    "record reference next to each fact you take from it, by its real "
    "reference exactly as shown (a PR number, a ticket key, or the decision "
    "id), never by the r1/r2 listing labels and never with a prefix the "
    "listing does not show. Everything "
    "the single-record rules forbid stays forbidden here: never invent "
    "values, names, files, or scope, and never state a DATE or number "
    "that is not written in the record text you were given or in the "
    "question; never "
    "affirm a figure that appears only in the question as a record fact; "
    "never open with a bare Yes or No, open with the outcome stated in "
    "the question's own terms so the first sentence cannot contradict "
    "the rest; if the records disagree, say which is newer and that they "
    "disagree rather than picking silently. When the question turns on a "
    "condition no record addresses (a plan type or contract term, a "
    "customer segment, an amount band, a party), do not extend a rule to "
    "the case no record names: reply with the sentinel below instead of "
    "answering and then noting that the records do not cover that case. "
    "The one-hop inference is a "
    "permission, not a mandate: make a leap ONLY when the records make it "
    "nearly certain. When the answer turns on a step the records do not "
    "themselves state (whether one record's field flows into another's "
    "output, whether a validation covers a case it does not name), do "
    "NOT resolve it with a confident yes or no: state what the records "
    "establish and name the exact remaining gap, so the reader sees the "
    "leap instead of trusting it. A record that only logs a request, "
    "proposal, or investigation ('looking into', 'proposal to', an open "
    "ticket) settles nothing; report what it proposes if useful, but "
    "never as a decision. A record marked NOT ADOPTED (cancelled, rejected, "
    "withdrawn, superseded, obsolete) is history: never state its content as "
    "what stands, and if only such records bear on the question, it is "
    "unanswered. If the records jointly still do not state or "
    "imply the needed answer, reply with exactly the sentinel RECORDS "
    "DO NOT ANSWER and nothing else. Two to four sentences, plain text, "
    "no preamble. No em dashes or en dashes. After the answer, on a line of "
    "its own, write COVERAGE: FULL when the records answer every part of "
    "the question, or COVERAGE: PARTIAL when any part stays open, rests on "
    "a proposal, or needs a step the records do not state."
)

EXPAND_SYSTEM = (
    "You expand one question into the search vocabulary an org's own "
    "records would use. The records are merged PR titles, commit messages, "
    "ticket bodies, and past signed answers; the question is often a "
    "paraphrase sharing almost none of their words. Return up to 8 short "
    "phrases of 2 to 4 words each: synonyms, the domain jargon an engineer "
    "would put in a ticket title, the concrete nouns behind the question's "
    "abstractions. Prefer vocabulary the question itself does NOT already "
    "use. Never invent org-specific names: no ticket refs, no person names, "
    "no service names you were not shown. Return ONLY JSON: "
    "{\"phrases\": [\"...\", \"...\"]}."
)

FOLLOWUP_CONTEXT_SYSTEM = (
    "You resolve references in a follow-up question so retrieval can work "
    "on it. You are given the last few answers Raven itself gave and one "
    "new question. If the question leans on that context ('your answer', "
    "'that ceiling', 'the same window', 'if so', 'that pause', 'what if it "
    "was X instead'), rewrite it as ONE "
    "self-contained question carrying the concrete facts it refers to, "
    "taken verbatim from the context shown. If it already stands alone, "
    "leave it untouched. You never answer the question and never add "
    "facts beyond the context shown. Return ONLY JSON: "
    "{\"needs_context\": true or false, \"question\": the rewritten "
    "question, or the original unchanged}. No em dashes or en dashes."
)

NAME_DECISIONS_SYSTEM = (
    "You are given a task as its requester wrote it, and what Raven already knows about the areas it "
    "touches. Name the decisions inside it that a person, not a coding agent, should settle: the points "
    "where the task leaves a real choice whose answer changes what users, operators or callers get. Each "
    "one must come from this task's own words. Quote or name the thing in the task it comes from, in "
    "`from`: a value the task leaves open, an existing thing it says to follow or match, a file or table it "
    "says to change, a behaviour it says to stop. If you cannot point at something in the task, do not name "
    "the decision. Write each as the question the person has to answer, specific to this change and not a "
    "template: \"Which tables does the migration have to cover?\" and not \"is this a schema change?\". Do "
    "not name work: writing the code, adding tests, updating docs and choosing names inside one file are "
    "not decisions for a person. Do not invent anything about the repository that the task does not say. "
    "Name at most five, fewest first, and name none at all for a task that leaves no real choice. Answer as "
    "lines and nothing else, two lines per decision and a blank line between them:\n"
    "DECISION: the question the person has to answer\n"
    "FROM: the words in the task it comes from\n"
    "Write no preamble, no numbering and no other lines. No em dashes or en dashes."
)


def name_decisions(cfg, title: str, statement: str, areas: list[str], owners: list[str]) -> list[dict]:
    """The decisions a model reads out of the task, each with the words
    in the task it came from. Empty without a backend.

    These are prompts for the agent beside the ones Raven's own signals
    produce, never a replacement for them: a wrong one costs the agent
    one read, and the reviewer's largest remaining gap is a real
    decision nobody writes down at all. Measured on the held-out Grafana
    tasks, the signal templates named 2 of the 8 decisions those changes
    turned on."""
    if not cfg or not cfg.semantic_retrieval or not (statement or "").strip():
        return []
    prompt = (f"TASK: {title}\n\nAS IT WAS GIVEN:\n{statement[:6000]}\n\n"
              f"AREAS BRIDGE KNOWS IT TOUCHES: {', '.join(areas[:6]) or 'none'}\n"
              f"PEOPLE WHO OWN THEM: {', '.join(owners[:6]) or 'nobody recorded'}")
    try:
        # Lines, not JSON: a task brief is full of backticked identifiers
        # and paths, and asking the model to quote them back inside a
        # JSON string broke the parse on two of the three held-out tasks,
        # which read as the model having nothing to say.
        text = Client(cfg.fast()).complete("name_decisions", NAME_DECISIONS_SYSTEM, prompt, max_tokens=1200)
    except LLMError:
        return []
    pairs: list[tuple[str, str]] = []
    question = ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.upper().startswith("DECISION:"):
            question = line.split(":", 1)[1].strip()
        elif line.upper().startswith("FROM:") and question:
            pairs.append((question, line.split(":", 1)[1].strip()))
            question = ""
    out = []
    low = (title + "\n" + statement).lower()
    for question, came_from in pairs[:5]:
        if not question or looks_meta(question) or len(question) < 12:
            continue
        # Grounding, checked rather than trusted: some word of what it
        # says the decision came from has to be in the task. Without
        # this it writes plausible decisions about repositories it has
        # not been told anything about.
        words = [w for w in re.findall(r"[A-Za-z_][\w.]{3,}", came_from.lower()) if w not in _STOP_FROM]
        if not came_from or not any(w in low for w in words):
            continue
        out.append({"question": question[:300], "from": came_from[:200]})
    return out


_STOP_FROM = {"this", "that", "task", "says", "the", "and", "with", "from", "into", "must", "should",
              "which", "what", "when", "where", "there", "their", "them", "then", "than", "task's"}


TEMPORAL_REVIEW_GUIDANCE = (
    "Requirements about once-only processing, duplicates, retries or ordered streams are temporal invariants. "
    "Distinguish an item being seen, attempted and successfully processed; follow exactly which history the "
    "authorized words require. Current membership or mutable state does not by itself prove that history. "
    "For an iterator, generator or callback allowed by the shown interface, trace permitted state changes "
    "between successive inputs, including a repeat after success and a repeat after an initial miss. Do not "
    "assume a fixed list or monotonic state unless the decision or code guarantees it. Identify the actual "
    "history guard or expose the limitation instead of treating a present-state check as once-only tracking. "
    "Do not invent unsupported concurrency or behavior outside the stated contract. "
)


REVIEW_OUTPUT_GUIDANCE = (
    "Keep the structured answer concise: at most 6000 characters of JSON total, requirement labels "
    "at most 160 characters, each explanation at most 240, and each quote at most 160. Quote a short "
    "exact line or an exact contiguous fragment, never a whole function. Do not print an analysis "
    "transcript, a path-by-path narrative, or explanations of successful checks. Examine every signed "
    "requirement and the relevant shown paths before composing the compact result. The output limits "
    "do not permit skipping requirements or assuming they pass. Set status to complete only if this "
    "stage examined its whole supplied scope and all findings fit; otherwise use inconclusive and name "
    "the remaining scope briefly in unexamined. Preserve concrete findings even when inconclusive. "
    "If the examination cannot be completed within the available budget, return an inconclusive JSON "
    "result promptly, rather than expanding the search or writing a long explanation. "
)


ALLEGATION_GUIDANCE = (
    "Judge observable behavior against the approved words, not your preferred implementation. Once-only, "
    "ordering and duplicate policies do not require a set, dict, list or any other tracking structure unless "
    "the approval explicitly says so; semantically equivalent implementations are acceptable. Every claimed "
    "behavioral violation, missing behavior, extra restriction or counterexample needs an allegation object: "
    "{\"kind\":\"behavioral\", \"authorized\":\"exact relevant quote from WHAT WAS AUTHORIZED\", "
    "\"input\":\"concrete initial state and permitted invocation\", \"sequence\":[\"ordered input/action\", "
    "\"permitted intervening state change, if any\"], \"expected\":\"behavior the quoted approval requires\", "
    "\"observed\":\"different behavior reached through the quoted code\"}. Use at most four short sequence "
    "steps; keep each field at most 160 characters. A label such as duplicates fail, a location, or absence of "
    "your preferred data structure is not a behavioral witness. Trace the stated sequence before alleging a "
    "departure: do not claim ordinary repeated inputs fail when the shown code actually handles them. "
    "For a genuinely explicit structural requirement only (a named file, signature, type or literal), use "
    "{\"kind\":\"structural\", \"authorized\":\"exact approval quote naming it\", "
    "\"required\":\"the exact literal structure named in that quote\", \"observed\":\"the shown mismatch\"}. "
    "Do not label a behavioral policy structural to avoid giving a witness. The row's at must locate the "
    "shown behavior or explicit structure. If you cannot supply this evidence, use unseen/inconclusive "
    "rather than inventing a preference or claiming a departure. "
)


SOURCE_REVIEW_GUIDANCE = (
    "APPROVED SOURCES are immutable source text supplied by Raven. Their IDs and character spans refer "
    "only to the original approval, never to your interpretation. Each requirement row must copy its "
    "source_id. Do not generate a needs label or replace the source with a paraphrase: the program supplies "
    "the original text. Multiple code checks may reference one source. Explicitly attest scope "
    "all_obligations for each fully examined source, separately from code-check counts. Examine every obligation within "
    "that source, including compound clauses, qualifications, types and distinctions between missing and "
    "explicit values. Code hints and individual checks do not narrow its scope. The entire original source "
    "and decision context govern each judgment. A source reference verifies provenance, not correctness. "
)


CONFORMANCE_SYSTEM = (
    "You are given one decision a person authorized, in their words, and a diff. Work in two steps and do "
    "not skip the first. " + TEMPORAL_REVIEW_GUIDANCE + REVIEW_OUTPUT_GUIDANCE + ALLEGATION_GUIDANCE + SOURCE_REVIEW_GUIDANCE +
    "FIRST, break the answer into the separate things it requires of the code: every "
    "named value, state, default, table, column, flag, file or behaviour it says the change must have. A "
    "sentence like \"three states, off, log and block, defaulting to off\" requires all three states AND the "
    "default, which is four requirements and not one. Mark each requirement \"must\" when the answer says "
    "the change has to do something, and \"must_not\" when it says the change has to avoid something (do not "
    "clamp in the application, do not keep the old path, nothing changes for anyone until an operator turns "
    "it on). SECOND, judge each requirement against the diff with one of four words. \"honored\": you can "
    "point at the lines that do what it requires or, for something it rules out, at lines that work on that "
    "very thing and keep clear of it. \"missing\": the diff works on that very thing and leaves this out or "
    "stops short of it. \"violated\": the diff does what the answer rules out, or does the required thing "
    "differently. \"unseen\": the diff does not go there at all. The words are about the person's decision, "
    "not about how the requirement is worded: \"POST is never retried after the socket connected\" is "
    "violated by a diff that retries it there, honored by a diff that keeps it from being retried, and unseen "
    "by a diff that never goes near retries. The difference between missing and unseen matters. A diff that "
    "edits the registry and puts one state where three were authorized has the other two missing; a diff "
    "that only fixes a typo in the docs has done nothing wrong, and every requirement about the registry is "
    "unseen. Getting the name and the shape of a thing right while leaving out the values it was supposed to "
    "take is not following it. Never count an intention, a comment, a test name or a TODO as the change "
    "itself: where one says a thing will be done and the code does not do it, that thing is missing. You are "
    "reading a diff somebody handed you, not the repository, so prefer \"unseen\" to assuming what the rest "
    "of the code does. The diff may also carry changes that other decisions on the same task authorized; when "
    "they are listed, those changes are neither requirements of this decision nor departures from it, so "
    "judge only what this one requires. For every requirement, copy into \"at\" the line of the diff you "
    "judged it by, exactly as it appears there and without the leading + or space: for honored, the line "
    "that does it, or for something ruled out the line that keeps clear of it; for violated, the line that "
    "does what was ruled out; for missing, the nearest line that works on that thing; empty for unseen. A "
    "requirement you cannot point at a line for is not honored. Do not give an overall verdict; report what "
    "you saw and it will be worked out from that. Return ONLY JSON: {\"schema\":\"source-checks-v1\", \"status\": \"complete\" or \"inconclusive\", "
    "\"unexamined\": \"\" or a short limitation, \"source_coverage\":[{\"source_id\":\"the supplied ID\", "
    "\"scope\":\"all_obligations\"}], \"requirements\": [{\"source_id\": \"the supplied ID\", "
    "\"kind\": \"must\" or \"must_not\", \"at\": \"the line\", \"found\": \"honored\" or \"missing\" or "
    "\"violated\" or \"unseen\", \"allegation\": null or the required evidence object}], "
    "\"why\": one sentence naming only substantiated differences or where you read it}. No em "
    "dashes or en dashes."
)


# The second reading. Measured live on eb9d22d: an owner signed "when time
# left is zero or negative, no further retry is permitted", the diff checked
# the budget in increment() and before every positive sleep, and the reading
# said honored. Two lines above the new check, a context line returned early
# when the backoff was zero, so an expired budget still let the next attempt
# through. Asked what the diff does, a reader finds the lines that do it;
# asked to break it, it has to walk the paths around them.
COUNTEREXAMPLE_SYSTEM = (
    "You are checking a reading of a diff, and your job is to break it. You get one decision a person "
    "authorized, in their words, the diff, and numbered original source scopes. First-reader code checks "
    "are hints, not replacements for those scopes. " + TEMPORAL_REVIEW_GUIDANCE + REVIEW_OUTPUT_GUIDANCE + ALLEGATION_GUIDANCE + SOURCE_REVIEW_GUIDANCE +
    "Some numbered rows are unsupported first-reader allegations, marked needs witness. Check them too. "
    "If such an allegation is refuted and the shown code honors the approval, return assessment honored "
    "with its exact code location. Never retain a preferred implementation as a requirement. "
    "Make one focused adversarial pass over every requirement. Challenge its cited line using relevant "
    "shown callers and branches, every early return or continue that can skip it, boundary values (zero, "
    "negative, None or null, empty input, exactly a limit), exception paths, and other shown places doing "
    "the same thing without that line. These are checks to apply where relevant, not a request to enumerate "
    "all combinations or to prove the whole program correct. Stop searching a requirement after one "
    "concrete counterexample and continue with the remaining requirements. Context lines count: the unchanged code "
    "around a change is the code it runs in. A counterexample is a concrete call or input for which the code "
    "shown does what the decision rules out or skips what it requires: say which call, with what value, and "
    "what the code then does. Something the decision itself allows is not a counterexample (when the answer "
    "says a setting is off by default, the default skipping the check is the decision), and neither is "
    "anything another listed decision authorized, style, naming, tests or docs. A later check that would "
    "catch it afterwards does not rescue a path: when the decision says something is refused or never done, "
    "the path that lets it happen once breaks it. When early exits are listed, go through them one by one: "
    "for each, work out when it is taken and what then happens that the requirements speak of. Report only "
    "exits with a concrete counterexample, identifying the requirement they break. Only report a path you can "
    "follow in the diff; when a requirement depends on code the diff does not show, such as a caller or a "
    "function defined elsewhere, name that code in not_shown instead of guessing what it does. Most "
    "requirements have no counterexample. In a source-bound reading the numbered scopes are ORIGINAL "
    "approved sources, not the first reader's paraphrases. Examine every obligation in each whole source. "
    "Every coverage row and finding must copy that numbered scope's source_id. Source coverage rows must "
    "also attest scope all_obligations; checking only selected code hints is incomplete. Return compact coverage for EVERY numbered requirement, not "
    "a narrative about each successful check. Coverage rows do not count toward the finding limit. Return "
    "each index exactly once in checks with assessment honored, unseen, or alleged; include at only when "
    "explicitly clearing an unsupported first-reader allegation. List every examined early-exit index in "
    "exits_checked. Put actionable allegations or missing-code details in a SEPARATE findings array with "
    "at most four distinct entries in total, including exit findings. An exit finding names both n and exit. "
    "Do not repeat a finding. All requirements and listed exits must be covered before status complete; "
    "unseen stays unknown and alleged needs a corresponding finding. If more than four distinct findings "
    "exist, keep the concrete evidence that fits, name the overflow in unexamined and use inconclusive. "
    "A positive coverage row must not hide or contradict a negative finding. Return ONLY JSON: "
    "{\"schema\":\"coverage-v2\", \"status\":\"complete\" or \"inconclusive\", "
    "\"unexamined\":\"\" or a short limitation, \"checks\":[{\"n\":1, "
    "\"source_id\":\"the supplied ID\", \"scope\":\"all_obligations\", \"assessment\":\"honored\" or \"unseen\" or \"alleged\", \"at\":optional exact code quote}], "
    "\"exits_checked\":[examined exit numbers], \"findings\":[{\"n\":requirement number, \"source_id\":\"the supplied ID\", "
    "\"exit\":optional exit number, \"at\":exact code quote, \"allegation\":the required evidence object "
    "or null, \"not_shown\":\"\" or missing code}]}. No em dashes or en dashes."
)


# A finite search policy for the same verified Sonnet 5 advisory API path.
# This additional search instruction is appended only on that API path.
COUNTEREXAMPLE_STOP_RULE = (
    " FINITE SEARCH: For every requirement within each original source, select at most three candidate traces from the relevant "
    "categories above, including permitted temporal state changes. Prefer the shortest traces most likely "
    "to break it. Settle each trace as a counterexample, not a counterexample, or dependent on missing code "
    "without repeatedly reconsidering it. Stop after one concrete counterexample or three candidate traces, "
    "then move to the next requirement. A compound source can contain multiple requirements; do not treat "
    "three traces for one clause as coverage of its other clauses. In addition, inspect each listed early exit once without enumerating "
    "alternative inputs. Emit the JSON after this pass, with no second verification pass. If a specific "
    "relevant path remains unresolved at the limit, name it in unexamined and use inconclusive. Report "
    "concrete findings even when other paths remain unresolved. Complete means this bounded pass finished "
    "for every requirement, not exhaustive path coverage or a proof of correctness. "
)


_DIFF_HEADER = re.compile(r"^(?:\+\+\+ (?:b/|/dev/null)|--- (?:a/|/dev/null)|@@|diff --git |index |new file mode |"
                          r"deleted file mode |similarity index |rename (?:from|to) )")


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


_TEST_PATH = re.compile(r"(?:^|/)(?:tests?|testing|spec|__tests__)/|(?:^|/)test_[^/]*$|_test\.\w+$|"
                        r"\.(?:spec|test)\.\w+$")


def diff_kept(diff: str, tests: bool = True) -> str:
    """The code a diff leaves in place, flattened for finding a quote in:
    added and context lines without their markers. Removed lines are what
    the change took out, not what it does, and headers are not code.
    With tests=False, files that are tests are left out too."""
    kept, current = [], ""
    for line in (diff or "").splitlines():
        header = re.match(r"^(?:diff --git a/\S+ b/(\S+)|\+\+\+ b/(\S+))", line)
        if header:
            current = header.group(1) or header.group(2)
        if _DIFF_HEADER.match(line) or line.startswith("-"):
            continue
        if not tests and _TEST_PATH.search(current):
            continue
        kept.append(line[1:] if line[:1] in ("+", " ") else line)
    return _flat(" ".join(kept))


_EXIT = re.compile(r"^\s*(?:return|continue|break|goto)\b|[:{)]\s*(?:return|continue|break)\b")
_GUARD = re.compile(r"^\s*(?:\}\s*)?(?:if|elif|else|unless|when|case|default|catch|except|finally)\b")


def early_exits(diff: str, limit: int = 12) -> list[str]:
    """The guarded early exits a diff shows, added or unchanged, in the
    files that are not tests: a return, continue or break under a
    condition, with the condition. These are the paths around a new check
    that a reader looking for the check does not walk."""
    out, current, prev = [], "", ""
    for line in (diff or "").splitlines():
        header = re.match(r"^(?:diff --git a/\S+ b/(\S+)|\+\+\+ b/(\S+))", line)
        if header:
            current, prev = header.group(1) or header.group(2), ""
            continue
        if line.startswith("@@"):
            prev = ""
            continue
        if _DIFF_HEADER.match(line) or line.startswith("-") or _TEST_PATH.search(current):
            continue
        code = (line[1:] if line[:1] in ("+", " ") else line).strip()
        if not code:
            continue
        if _EXIT.search(code):
            if _GUARD.match(code) or re.match(r"^\s*if\b", code):
                out.append(code)
            elif prev and _GUARD.match(prev):
                out.append(f"{prev} -> {code}")
        prev = code
    seen, unique = set(), []
    for exit_ in out:
        if exit_ not in seen:
            seen.add(exit_)
            unique.append(clip_marked(exit_, 200, "the diff has the rest"))
    return unique[:limit]


def focus_diff(diff: str, paths: list[str]) -> str:
    """The diff with the files a decision is about first, the rest of the
    code next, and tests last, so what a reading is given first is what
    the decision governs. Measured live on 5e967e4: a 37,000-character diff
    was read to its first 20,000, changelog and tests in front."""
    sections = re.split(r"(?m)^(?=diff --git )", diff or "")
    files = [x for x in sections if x.startswith("diff --git ")]
    if len(files) < 2:
        return diff or ""
    lead = "".join(x for x in sections if not x.startswith("diff --git "))
    wanted = [p.strip().strip("/") for p in paths if p and p.strip() and p.strip() != "unknown"]

    def rank(section: str) -> int:
        m = re.match(r"diff --git a/\S+ b/(\S+)", section)
        path = m.group(1) if m else ""
        if any(path == p or path.startswith(p + "/") or (p.endswith("*") and path.startswith(p.rstrip("*")))
               for p in wanted):
            return 0
        return 2 if _TEST_PATH.search(path) else 1
    return lead + "".join(sorted(files, key=rank))


def _unread_files(diff: str, limit: int) -> list[str]:
    """The files of a diff that lie wholly or partly past `limit`."""
    out, at = [], 0
    for section in re.split(r"(?m)^(?=diff --git )", diff or ""):
        m = re.match(r"diff --git a/\S+ b/(\S+)", section)
        if m and at + len(section) > limit:
            out.append(m.group(1) if at >= limit else f"{m.group(1)} (in part)")
        at += len(section)
    return out


# A requirement about which files the change has: their names, where they
# live, whether one is added, moved or removed. Its evidence is the diff's
# own file headers, which the code-only view leaves out.
_STRUCTURAL_RE = re.compile(r"\b(?:file ?names?|files?|fragments?|paths?|director(?:y|ies)|folders?|pages?|modules?|"
                            r"renam\w*|mov(?:e|ed|es)|delet\w*|new file|changelog|readme)\b|"
                            r"\b[\w-]+(?:/[\w.-]+)*\.(?:py|rst|md|txt|go|js|ts|tsx|c|h|cc|rs|java|json|ya?ml|toml|cfg|ini)\b",
                            re.IGNORECASE)


def diff_paths(diff: str) -> set[str]:
    """Every file path a diff names in its headers."""
    out: set[str] = set()
    for line in (diff or "").splitlines():
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            out.update(m.groups())
            continue
        m = re.match(r"^(?:\+\+\+ b/|--- a/|rename (?:from|to) )(\S+)", line)
        if m:
            out.add(m.group(1))
    return out


def located_header(quote: str, diff: str) -> bool:
    """Whether a quote is one of the diff's own file headers, or names a
    file the diff changes. Measured live on 63eb671: the reading quoted
    `diff --git a/changelog/backoff-deadline.feature.rst ...` for "use a
    descriptive unassigned filename", the line was in the diff, and the
    check said it was not: it looked only at the code."""
    raw = _flat((quote or "").strip().strip("`"))
    if not raw:
        return False
    headers = {_flat(line) for line in (diff or "").splitlines() if _DIFF_HEADER.match(line)}
    if raw in headers or _flat(re.sub(r"^[+ ]", "", raw)) in headers:
        return True
    paths = diff_paths(diff)
    return any(re.sub(r"^[ab]/", "", token) in paths for token in raw.split())


def located(quote: str, kept: str) -> bool:
    """Whether a line a model quoted is in the code the diff leaves, as
    written. A marker pasted with it and differences in spacing are
    forgiven, and an elision ("...") matches when the pieces around it
    appear in order; a paraphrase is not a quote."""
    lines = [re.sub(r"^[+ ]", "", line) for line in (quote or "").strip().strip("`").splitlines()]
    pieces = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", _flat(" ".join(lines))) if p]
    if not pieces or sum(len(p) for p in pieces) < 6:
        return False
    at = 0
    for piece in pieces:
        found = kept.find(piece, at)
        if found < 0:
            return False
        at = found + len(piece)
    return True


UNSTATED_CONDITIONS_SYSTEM = (
    REVIEW_OUTPUT_GUIDANCE + ALLEGATION_GUIDANCE +
    "Read the authorized decisions and the diff. Find additional policy conditions or exemptions "
    "the change introduces which none of those decisions authorizes. Look for extra customer, plan, "
    "region, account or feature-flag branches that change who gets the behavior. Do not invent "
    "requirements, treat ordinary validation as policy, or report conditions that the other signed "
    "decisions allow. Quote an exact line of production code for each finding. Return ONLY JSON: "
    '{"status": "complete" or "inconclusive", "unexamined": "" or a short limitation, '
    '"conditions": [{"condition": "the extra condition and its effect", "at": "exact code line", '
    '"allegation": the required evidence object}]}. '
    "Return at most four conditions; if more exist, retain concrete findings and use inconclusive. "
    "Return an empty conditions list when there is no such addition. Test expectations alone are not evidence."
)


def _unstated_conditions(reader, context: str, shown: str, code: str, seen: list[dict],
                         answer: str = '') -> tuple[list[str], bool]:
    try:
        raw = Client(reader).complete_json('conditions', UNSTATED_CONDITIONS_SYSTEM,
                                           context + "\nDIFF:\n" + shown, max_tokens=REVIEW_MAX_TOKENS, bounded=True)
    except LLMError:
        raw = None
    rows = raw.get('conditions') if isinstance(raw, dict) else None
    conditions = [row for row in rows if _condition_shape(row)] if isinstance(rows, list) else []
    complete = (_review_complete(raw, 'conditions') and len(conditions) == len(rows)
                and len(rows) <= REVIEW_MAX_FINDINGS)
    unexamined = []
    if not complete:
        limitation = _review_limitation(raw, 'the extra-condition search did not complete')
        seen.append({'needs': 'No additional policy conditions without authorization', 'kind': 'must_not',
                     'found': 'unseen', 'state': 'unclear', 'note': limitation})
        unexamined.append(limitation)
    for condition in conditions[:REVIEW_MAX_FINDINGS]:
        what = clip_marked(str(condition.get('condition') or ''), 500, 'the condition ran on')
        at = clip_marked(str(condition.get('at') or ''), 300, 'the line ran on')
        if not what:
            continue
        allegation = _grounded_allegation(condition.get('allegation'), answer, at, code, shown)
        seen.append({'needs': allegation['authorized'] if allegation else what, 'kind': 'must_not',
                     'found': 'violated' if allegation else 'unseen',
                     'state': 'departs' if allegation else 'unclear', 'at': at,
                     **({'allegation': allegation} if allegation else {}),
                     'note': 'the diff adds a condition no signed answer states' if allegation else
                             'the extra-condition allegation lacks a grounded behavioral witness'})
        if not allegation:
            unexamined.append('an extra-condition allegation lacks a grounded behavioral witness')
    return unexamined, complete


def _approved_sources(answer):
    """A conservative exact span: do not guess semantic clause boundaries.

    Punctuation, inline code, examples and compound clauses can change meaning
    when split. The whole signed answer is currently one immutable source unit.
    Models may decompose code checks, but cannot decompose away this scope.
    """
    if not isinstance(answer, str) or not answer.strip():
        return []
    digest = hashlib.sha256(answer.encode('utf-8', errors='surrogatepass')).hexdigest()
    return [{'id': f'answer-{digest[:16]}-0-{len(answer)}', 'start': 0, 'end': len(answer), 'text': answer}]


def _source_binding_report(raw, sources):
    """Validate source references; never certify natural-language entailment."""
    known = {source['id']: source for source in sources}
    rows = raw.get('requirements') if isinstance(raw, dict) else None
    report = {'complete': False, 'bindings': [], 'missing': [], 'issues': []}
    issues, covered, attested = report['issues'], set(), set()
    if not isinstance(raw, dict) or raw.get('schema') != 'source-checks-v1':
        issues.append('schema: expected source-checks-v1')
    if not _review_complete(raw, 'requirements'):
        issues.append('requirements: no complete bounded examination')
    if not isinstance(rows, list) or not rows:
        issues.append('requirements: expected a nonempty list')
        rows = []
    attestations = raw.get('source_coverage') if isinstance(raw, dict) else None
    if not isinstance(attestations, list):
        issues.append('source_coverage: expected explicit whole-source attestations')
        attestations = []
    for index, entry in enumerate(attestations):
        source_id = entry.get('source_id') if isinstance(entry, dict) else None
        if not isinstance(source_id, str) or source_id not in known:
            issues.append(f'source_coverage[{index}].source_id: missing or unknown approved source')
        elif entry.get('scope') != 'all_obligations' or set(entry) != {'source_id', 'scope'}:
            issues.append(f'source_coverage[{index}]: expected scope all_obligations')
        else:
            attested.add(source_id)
    for index, row in enumerate(rows):
        path = f'requirements[{index}]'
        source_id = row.get('source_id') if isinstance(row, dict) else None
        source = known.get(source_id) if isinstance(source_id, str) else None
        valid = source is not None and _requirement_shape(row)
        if source is None:
            issues.append(path + '.source_id: missing or unknown approved source')
        elif not _requirement_shape(row):
            issues.append(path + ': malformed code check')
        # Older labels and optional echoes may not narrow or paraphrase the
        # whole source. Absence is safe: runtime supplies the exact text.
        for key in ('needs', 'source_text'):
            if isinstance(row, dict) and key in row and source is not None and row[key] != source['text']:
                issues.append(path + '.' + key + ': does not match the whole approved source')
                valid = False
        report['bindings'].append({'row': index, 'source_id': source_id if source else '', 'valid': valid})
        if valid:
            covered.add(source_id)
    report['missing'] = [source_id for source_id in known if source_id not in covered or source_id not in attested]
    if report['missing']:
        issues.append(f'approved sources: {len(report["missing"])} sources lack valid checks or whole-source attestations')
    report['complete'] = not issues
    return report


def _source_counterexamples(reader, context, shown, code, seen, sources, answer):
    """Give the critic original source scopes independently of decomposition.

    A single source may have several first-reader code checks. Challenge its
    full text once, retain evidence once, and propagate uncertainty to its rows.
    """
    scopes = []
    for source in sources:
        peers = [row for row in seen if row.get('source_id') == source['id'] and row.get('source_status') == 'bound']
        scope = {'source_id': source['id'], 'needs': source['text'], 'kind': 'must', 'state': 'ok',
                 'at': next((row['at'] for row in peers if row.get('at')), '')}
        scope['code_checks'] = [{key: row[key] for key in ('at', 'kind', 'found') if key in row} for row in peers]
        if any(row.get('allegation_status') == 'needs_witness' for row in peers):
            scope['allegation_status'] = 'needs_witness'
        scopes.append(scope)
    limitations, complete = _counterexamples(reader, context, shown, code, scopes, answer, sources=sources)
    for scope in scopes:
        peers = [row for row in seen if row.get('source_id') == scope['source_id'] and row.get('source_status') == 'bound']
        if not peers:
            # Missing decomposition cannot be repaired into a complete first
            # reading; keep any independently grounded critic evidence visible.
            peers = [{'needs': clip_marked(scope['needs'], 400, 'full text is in approved_sources'),
                      'kind': 'must', 'found': 'unseen', 'state': 'unclear',
                      'source_id': scope['source_id'], 'source_status': 'unexamined'}]
            seen.extend(peers)
        if scope['state'] != 'ok':
            for row in peers:
                if row['state'] != 'departs':
                    row['state'] = 'unclear'
                if scope.get('note'):
                    row['note'] = clip_marked('; '.join(dict.fromkeys(filter(None, (row.get('note'), scope['note'])))),
                                              500, 'additional limitations are listed separately')
        elif scope.get('note') == 'the critic rejected an unsupported first-reader allegation':
            for row in peers:
                if row.get('allegation_status') == 'needs_witness':
                    row.update(state='ok', found='honored', at=scope['at'], note=scope['note'])
                    row.pop('allegation_status', None)
        for key in ('counterexample', 'counterexamples', 'not_shown', 'not_shown_details'):
            if key in scope:
                peers[0][key] = scope[key]
                if key == 'counterexample':
                    peers[0].pop('allegation_status', None)
    return limitations, complete


def check_conformance(cfg, question: str, answer: str, diff: str, others: tuple = ()) -> dict:
    """Whether a diff does what one authorized decision says, as
    {verdict, why, requirements, unexamined, incomplete}. Empty when
    disabled; an unsuccessful model reading is explicitly inconclusive.

    Three readings. The first breaks the answer into requirements and
    quotes the line of the diff each is judged by; a requirement read as
    met with no such line in the diff is not met. The second tries to
    break every requirement read as met, walking the paths around the
    quoted line, and one it finds a counterexample for reads as unclear
    with the counterexample beside it. What a requirement depends on that
    the diff does not show is listed as unexamined, never assumed. A third
    reading looks for additional policy conditions no signed answer authorizes.

    This reports; it never gates. Raven gates authorization, and the
    thing it has never been able to say is whether the code that came
    out the other end follows what was signed. Saying so from a diff the
    agent supplied is not verification either, and the wording that
    carries this has to keep saying so.

    The verdict is recomputed here from the requirements rather than
    taken on the model's word. Measured on the Grafana runs, asked
    straight out it called a change that added the toggle with the right
    name and stage, and one of the three values the owner signed for,
    "follows". A false follows is worse than no report at all: it
    manufactures confidence about the one thing Raven has always been
    careful to say it cannot check."""
    if not cfg or not cfg.semantic_retrieval or not (diff or "").strip():
        return {}
    sources = _approved_sources(answer)
    shown = diff[:DIFF_READ]
    kept = diff_kept(shown)
    production = diff_kept(shown, tests=False)
    # Function context may be in a hunk header rather than a kept line.
    production += ' ' + ' '.join(line.split('@@')[-1] for line in shown.splitlines()
                                 if line.startswith('@@'))
    # The main model, not the fast one. Measured on evals/newdev/conformance
    # with the real urllib3 diffs added: the fast model read a correct
    # patch as departing from what was signed once in every run, the main
    # model never did. It runs once per signed decision at the finish.
    reader = cfg.fast() if os.environ.get("BRIDGE_CONFORMANCE_MODEL", "main") == "fast" else cfg
    try:
        shared = _shared_decisions(others)
    except (TypeError, ValueError):
        return _inconclusive_review('the shared approved decision context is malformed', sources=sources)
    if not isinstance(question, str):
        return _inconclusive_review('the original decision question is malformed', sources=sources)
    approved_context = {'question': question, 'other_decisions': shared}
    context = (f"DECISION: {question}\nWHAT WAS AUTHORIZED:\n"
               + "APPROVED SOURCES (exact text; offsets are Python character positions):\n"
               + json.dumps(sources, ensure_ascii=False) + '\n' + _other_decisions(others))
    if not sources:
        return _inconclusive_review('the exact approved source is empty or malformed',
                                    sources=sources, context=approved_context)
    if len(context) > REVIEW_CONTEXT_MAX_CHARS:
        return _inconclusive_review('the exact approved context exceeds the bounded source-context limit',
                                    sources=sources, context=approved_context)
    try:
        raw = Client(reader).complete_json("conformance", CONFORMANCE_SYSTEM, context + f"\nDIFF:\n{shown}",
                                           max_tokens=REVIEW_MAX_TOKENS, bounded=True)
    except LLMError:
        raw = None
    raw_reqs = raw.get('requirements') if isinstance(raw, dict) else None
    reqs = [r for r in raw_reqs if _requirement_shape(r)] if isinstance(raw_reqs, list) else []
    binding = _source_binding_report(raw, sources)
    valid_rows = {id(raw_reqs[row['row']]): row['source_id'] for row in binding['bindings'] if row['valid']}
    known_sources = {source['id']: source for source in sources}
    first_complete = binding['complete']
    if not reqs:
        result = _inconclusive_review('the requirement reading did not complete')
        result['approved_sources'] = sources
        result['approved_context'] = approved_context
        result['source_issues'] = binding['issues'][:8]
        result['source_issues_omitted'] = max(0, len(binding['issues']) - 8)
        return result
    # The model reports what it saw; the verdict is worked out here. Told
    # to judge directly it blessed a change that dropped two of the three
    # values an owner signed for, and told to weigh its own observations
    # it marked "do not clamp in the application" unseen and went
    # uncertain on a change that plainly did what was asked.
    seen = []
    for r in reqs:
        source = known_sources.get(valid_rows.get(id(r)))
        source_text = source['text'] if source else str(r.get('needs', ''))
        found = str(r.get("found", "")).strip().lower()
        forbids = str(r.get("kind", "must")).strip().lower() == "must_not"
        at = clip_marked(str(r.get("at") or ""), 300, "the line ran on")
        note = ""
        # "honored" and "violated" keep their sense whichever way the
        # requirement is worded. The words before them were present, missing
        # and unseen, with "present" on a must_not meaning the forbidden
        # thing was done, and they are still read that way.
        # Measured on the hard end-to-end run: the model wrote "POST/PATCH
        # must not be retried after connect" as a must_not, found that rule
        # in the diff, said "present", and a change that did what was signed
        # read as departing from it.
        if found in ("honored", "violated"):
            state = "ok" if found == "honored" else "departs"
        elif found == "missing" and forbids:
            # Something ruled out, "missing": the forbidden thing, or the
            # rule against it? Either reading is a guess.
            state = "unclear"
        elif forbids:
            # Not doing a forbidden thing is the requirement being met.
            state = "departs" if found == "present" else "ok"
        else:
            state = {"present": "ok", "missing": "departs", "unseen": "unclear"}.get(found, "unclear")
        if state == "departs" and found == "missing":
            named_calls = set(re.findall(r'\b([A-Za-z_]\w*)\(\)', source_text))
            absent = sorted(name for name in named_calls
                            if not re.search(r'\b' + re.escape(name) + r'\s*\(', production))
            if absent:
                # A real host's increment() delegated to new(), but its unchanged
                # body was not in the diff. The reader invented a missing params
                # update there. Tests mentioning the function are not its body.
                state = "unclear"
                note = "missing behavior was claimed in code the diff does not show: " + ', '.join(name + '()' for name in absent)
        if state == "ok" and found in ("honored", "present") and not located(at, kept) \
                and not (_STRUCTURAL_RE.search(source_text) and located_header(at, shown)):
            # Honored is a claim about lines, and the line has to be there.
            # Measured live on eb9d22d: "when time left is zero or negative,
            # no further retry is permitted" read as honored with nothing to
            # point at, and the change let the next attempt through. Something
            # ruled out and nowhere in the diff is the diff not doing it.
            if forbids:
                note = "no line of the diff quoted for it; read as the diff not doing it"
            else:
                state = "unclear"
                note = ("the line quoted for it is not in the diff" if at.strip()
                        else "read as honored with no line of the diff to show for it")
        allegation = None
        unsupported_allegation = state == 'departs'
        if state == 'departs':
            allegation = _grounded_allegation(r.get('allegation'), answer, at, production, shown)
            if allegation is None:
                state = 'unclear'
                note = 'departure was claimed without a grounded behavioral witness or explicit structural requirement'
            else:
                unsupported_allegation = False
        source_id = valid_rows.get(id(r))
        source = known_sources.get(source_id)
        if source is None and state == 'ok':
            state = 'unclear'
        if source is None:
            note = 'the code check is not bound to the whole verbatim approved source'
        item = {"needs": clip_marked(source['text'] if source else str(r.get("needs", "Unbound code check")),
                                    400, "full text is in approved_sources"),
                "kind": "must_not" if forbids else "must", "found": found, "state": state}
        item['source_status'] = 'bound' if source else 'unbound'
        if source:
            item['source_id'] = source_id
        if at.strip():
            item["at"] = at
        if allegation is not None:
            if source is None:
                item['needs'] = allegation['authorized']
            item['allegation'] = allegation
        if unsupported_allegation:
            item['allegation_status'] = 'needs_witness'
        if note:
            item["note"] = note
        seen.append(item)
    unexamined = []
    if not first_complete:
        limitation = _review_limitation(raw, 'the requirement reading did not complete')
        unexamined.append(limitation)
        unexamined.extend(binding['issues'][:6])
        seen.append({'needs': 'Every signed requirement examined', 'kind': 'must', 'found': 'unseen',
                     'state': 'unclear', 'note': limitation})
    if len(diff) > DIFF_READ:
        cut = _unread_files(diff, DIFF_READ)
        unexamined.append(f"the diff past its first {DIFF_READ} characters ({len(diff)} given)"
                          + (f": {', '.join(cut[:8])}" + (f" and {len(cut) - 8} more" if len(cut) > 8 else "")
                             if cut else ""))
    complete = first_complete
    if any(row['state'] == 'ok' or row.get('allegation_status') == 'needs_witness'
           or row.get('source_status') == 'unbound' and row.get('found') in ('honored', 'present') for row in seen):
        limitations, stage_complete = _source_counterexamples(reader, context, shown, diff_kept(shown, tests=False),
                                                             seen, sources, answer)
        unexamined += limitations
        complete = complete and stage_complete
    if any(item['state'] == 'ok' for item in seen):
        limitations, stage_complete = _unstated_conditions(reader, context, shown, diff_kept(shown, tests=False), seen, answer)
        unexamined += limitations
        complete = complete and stage_complete
    states = {s["state"] for s in seen}
    verdict = "departs" if "departs" in states else ("unclear" if "unclear" in states or unexamined else "follows")
    # Whole, or cut at a word and marked. Measured live on a397f1c: five of
    # seven reasons ended mid-word at 300 characters ("used by sleep_for_r").
    why = str(raw.get("why", "")).strip()
    if any(r.get('found') in ('missing', 'violated', 'present') for r in reqs):
        # The free-text reason may invent an implementation mandate. Rebuild
        # negative summaries solely from the evidence that survived grounding.
        supported = [item['allegation'] for item in seen if item.get('allegation')]
        why = '; '.join(_allegation_summary(item) for item in supported)
        if not why and any(item.get('allegation_status') == 'needs_witness' for item in seen):
            why = 'The claimed departure lacks a grounded behavioral witness or explicit structural requirement.'
    unsupported = [s for s in seen if s.get("note", "").startswith("missing behavior was claimed")]
    if unsupported and verdict == "unclear":
        why = "The diff does not establish whether these requirements are met: " + '; '.join(s["needs"] for s in unsupported)
    if not first_complete:
        why = "The requirement reading is inconclusive. " + why
    if any(s.get("note") == "counterexample search did not complete" for s in seen):
        why = "The counterexample search did not complete; the first reading is inconclusive. " + why
    if not why:
        # Measured live: two "follows" readings came back with no reason at
        # all. Name the requirements the verdict rests on.
        state = {"follows": "ok", "departs": "departs", "unclear": "unclear"}[verdict]
        named = list(dict.fromkeys(s["needs"] for s in seen if s["state"] == state and s["needs"]))
        lead = {"follows": "Read as doing each thing it requires", "departs": "Read as departing from it on",
                "unclear": "The diff does not show"}[verdict]
        why = f"{lead}: {'; '.join(named)}" if named else ""
    countered = [s for s in seen if (s.get("counterexample") or {}).get("located")]
    if countered:
        # The counterexample leads: the reason the first reading gave is
        # the one it was found against.
        c = countered[0]
        where = f" (at `{c['counterexample']['at']}`)" if c["counterexample"].get("at") else ""
        why = (f"Possible counterexample to \"{c['needs']}\": {c['counterexample']['what']}{where}. "
               + (f"The first reading: {why}" if why else "")).strip()
    additions = [_allegation_summary(item['allegation']) for item in seen
                 if item.get('note') == 'the diff adds a condition no signed answer states']
    if additions:
        why = 'The diff adds a condition no signed answer states: ' + '; '.join(additions)
    if verdict == 'unclear' and not countered:
        # Uncertainty can come from a missing quote with no unexamined entry.
        # Do not repeat the model's unqualified all-supported summary after
        # deterministic checks have contradicted it.
        unresolved = [f"{item['needs']}: {item.get('note') or item.get('not_shown') or 'not established by the shown diff'}"
                      for item in seen if item['state'] == 'unclear']
        limitations = list(dict.fromkeys([*unexamined, *unresolved]))
        why = 'The review is inconclusive: ' + '; '.join(limitations[:4] or ['the shown diff does not establish conformance'])
    why = clip_marked(why, 1200, "the requirements list what was read")
    out = {"verdict": verdict, "why": why, "requirements": seen, "approved_sources": sources,
           "approved_context": approved_context}
    if binding['issues']:
        out['source_issues'] = binding['issues'][:8]
        out['source_issues_omitted'] = max(0, len(binding['issues']) - 8)
    if not complete:
        out['incomplete'] = True
    if unexamined:
        out["unexamined"] = unexamined
    return out


# How much of a diff one reading is given. What lies past it was not read,
# and the reading says so.
DIFF_READ = 20000


def _review_complete(raw, collection):
    """Only an explicit, bounded completion can support an all-clear."""
    return (isinstance(raw, dict) and raw.get('status') == 'complete'
            and isinstance(raw.get(collection), list)
            and isinstance(raw.get('unexamined', ''), str) and not raw.get('unexamined', '').strip()
            and len(json.dumps(raw, ensure_ascii=False)) <= REVIEW_MAX_CHARS)


def _review_limitation(raw, fallback):
    value = raw.get('unexamined') if isinstance(raw, dict) else None
    return clip_marked(value, 300, 'the limitation ran on') if isinstance(value, str) and value.strip() else fallback


def _inconclusive_review(reason, *, sources=None, context=None):
    result = {'verdict': 'unclear', 'why': reason + '; the advisory reading is inconclusive.',
              'requirements': [], 'unexamined': [reason], 'incomplete': True}
    if sources is not None:
        result['approved_sources'] = sources
    if context is not None:
        result['approved_context'] = context
    return result


def _grounded_allegation(raw, answer, at, code, shown):
    """Check a behavioral witness or an explicitly authorized literal.

    This validates provenance and structure, never executes the supplied diff
    or claims the described behavior was independently reproduced.
    """
    if not isinstance(raw, dict) or raw.get('kind') not in ('behavioral', 'structural'):
        return None
    quote = raw.get('authorized')
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 300:
        return None
    if (_flat(quote) not in _flat(answer) or not _flat(answer)
            or len(_flat(quote)) < min(12, len(_flat(answer)))):
        return None
    fields = ('input', 'expected', 'observed') if raw['kind'] == 'behavioral' else ('required', 'observed')
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() or len(raw[key]) > 300 for key in fields):
        return None
    result = {'kind': raw['kind'], 'authorized': quote.strip(), **{key: raw[key].strip() for key in fields}}
    if raw['kind'] == 'behavioral':
        steps = raw.get('sequence')
        if (not isinstance(steps, list) or not 1 <= len(steps) <= 4
                or any(not isinstance(step, str) or not step.strip() or len(step) > 200 for step in steps)
                or _flat(raw['expected']) == _flat(raw['observed']) or not _located_allegation(at, code)):
            return None
        result['sequence'] = [step.strip() for step in steps]
    else:
        literal = raw['required'].strip().strip('`')
        if (not _explicit_structure(quote, literal) or _flat(literal) == _flat(raw['observed'])
                or not (_located_allegation(at, code) or (_STRUCTURAL_RE.search(quote) and located_header(at, shown)))):
            return None
    return result


def _located_allegation(at, code):
    if not isinstance(at, str) or len(at) > 300:
        return False
    quote = _flat(' '.join(re.sub(r'^[+ ]', '', line) for line in at.strip().strip('`').splitlines()))
    return len(quote) >= 6 and quote in code


def _explicit_structure(quote, literal):
    """Conservative source syntax, not a proof that the allegation is true."""
    if not literal or len(literal) > 120 or not re.search(r'(?<!\w)' + re.escape(literal) + r'(?!\w)', quote):
        return False
    # Identifier/path/signature syntax must be a literal, not a sentence
    # containing punctuation such as "ignore later duplicates.".
    if (re.fullmatch(r"[\w./()\[\],:*=-]+", literal) and re.search(r'[/_()]|\w\.\w', literal)):
        return True
    named = re.search(r'\b(?:named|name|signature|file|path|column|table|format|states|values|default|literal|string)\b'
                      r'[^.;:]{0,60}(?<!\w)' + re.escape(literal) + r'(?!\w)', quote, re.IGNORECASE)
    types = {'set', 'dict', 'dictionary', 'list', 'tuple', 'array', 'map', 'integer', 'int', 'float',
             'boolean', 'bool', 'string', 'str'}
    typed = literal.lower() in types and re.search(r'\b(?:use|using|return|returns|type)\s+(?:(?:a|an|the)\s+)?'
                                                    + re.escape(literal) + r'\b', quote, re.IGNORECASE)
    return bool(named or typed)


def _allegation_summary(allegation):
    if allegation['kind'] == 'structural':
        return f"Approved: {allegation['authorized']}; shown: {allegation['observed']}"
    return (f"{allegation['input']}; " + '; '.join(allegation['sequence'])
            + f". Expected: {allegation['expected']}; observed: {allegation['observed']}")


def _requirement_shape(row):
    return (isinstance(row, dict)
            and (isinstance(row.get('needs'), str) and bool(row['needs'].strip())
                 or isinstance(row.get('source_id'), str) and bool(row['source_id']))
            and row.get('kind') in ('must', 'must_not')
            and row.get('found') in ('honored', 'missing', 'violated', 'unseen', 'present')
            and isinstance(row.get('at', ''), str))


def _condition_shape(row):
    return (isinstance(row, dict) and isinstance(row.get('condition'), str) and bool(row['condition'].strip())
            and isinstance(row.get('at'), str))


def _has_finding(value):
    return bool(value.strip()) and value.strip().lower().rstrip('.') not in ('none', 'no', 'n/a')


def _review_index(value, limit, zero=False):
    """Normalize bounded legacy numbers without bools, floats or huge ints."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str):
        digits = value.strip().rstrip('.')
        if len(digits) > 20 or not re.fullmatch(r'[0-9]+\.?', value.strip()):
            return None
        number = int(digits)
    else:
        number = value
    return number if (0 if zero else 1) <= number <= limit else None


def _counterexample_report(raw, requirements, exits, sources=None):
    """Normalize coverage and actionable claims separately, before any cap.

    Issues contain only controlled paths, numeric indices and fixed messages;
    safe observers can retain this report without transport bodies or prompts.
    Legacy sparse replies require their explicit complete attestation. New
    coverage-v2 replies additionally account for every supplied requirement
    and early exit. This is protocol validation, not semantic verification.
    """
    exit_lines = exits if isinstance(exits, list) else None
    exit_count = len(exits) if exit_lines is not None else exits
    report = {'mode': 'invalid', 'complete': False, 'checks': [], 'findings': [], 'issues': [],
              'conflicts': [], 'counts': {'requirements': requirements, 'listed_exits': exit_count, 'exit_rows': 0,
                                        'check_rows': 0, 'finding_rows': 0, 'duplicate_checks': 0,
                                        'duplicate_findings': 0, 'distinct_findings': 0}}
    issues = report['issues']
    expected_sources = [source['id'] for source in sources] if sources is not None else None
    def issue(path, problem):
        value = f'{path}: {problem}'
        if value not in issues:
            issues.append(value)
    if not isinstance(raw, dict):
        issue('reply', 'expected an object')
        return report
    indexed = 'schema' in raw or 'findings' in raw or 'exits_checked' in raw
    report['mode'] = 'coverage-v2' if indexed else 'legacy-sparse'
    if expected_sources is not None and not indexed:
        issue('schema', 'source-bound coverage requires coverage-v2 with explicit source IDs')
    if indexed and raw.get('schema') != 'coverage-v2':
        issue('schema', 'expected coverage-v2')
    if raw.get('status') != 'complete':
        issue('status', 'no explicit complete attestation')
    if not isinstance(raw.get('unexamined', ''), str):
        issue('unexamined', 'expected a string')
    elif raw.get('unexamined', '').strip():
        issue('unexamined', 'declared unresolved scope')
    if len(json.dumps(raw, ensure_ascii=False)) > REVIEW_MAX_CHARS:
        issue('reply', f'exceeds {REVIEW_MAX_CHARS} normalized JSON characters')
    allowed = {'schema', 'status', 'unexamined', 'checks', 'findings', 'exits_checked'} if indexed else {
        'status', 'unexamined', 'checks', 'exits'}
    if set(raw) - allowed:
        issue('reply', 'unexpected fields')
    checks = raw.get('checks')
    if not isinstance(checks, list):
        issue('checks', 'expected a list')
        checks = []
    report['counts']['check_rows'] = len(checks)
    coverage, actions, conflicts = {}, [], set()

    def bind_source(row, n, entry, path):
        if expected_sources is None:
            return
        expected = expected_sources[n - 1]
        if row.get('source_id') != expected:
            issue(path + '.source_id', 'missing or mismatched approved source')
        # n identifies the expected scope; a malformed source echo cannot
        # promote a pass. Independently grounded partial evidence may survive.
        entry['source_id'] = expected

    def action(row, path, exit_number=None):
        if not isinstance(row, dict):
            issue(path, 'expected an object')
            return
        n = _review_index(row.get('n'), requirements)
        if n is None:
            issue(path + '.n', f'expected an index in 1..{requirements}')
            return
        entry = {'n': n}
        bind_source(row, n, entry, path)
        if exit_number is None and 'exit' in row:
            exit_number = _review_index(row['exit'], exit_count)
            if exit_number is None:
                issue(path + '.exit', f'expected an index in 1..{exit_count}')
                # An invalid optional exit cannot erase independently located evidence.
        for key, limit in (('at', 300), ('counterexample', 500), ('not_shown', 300)):
            if key in row:
                if not isinstance(row[key], str) or len(row[key]) > limit:
                    issue(path + '.' + key, f'expected a string of at most {limit} characters')
                    continue
                if row[key].strip() and (key != 'not_shown' or _has_finding(row[key])):
                    entry[key] = row[key].strip()
        if row.get('allegation') is not None:
            if not isinstance(row['allegation'], dict):
                issue(path + '.allegation', 'expected an object or null')
            else:
                # Ignore arbitrary extra keys in a model object; they are not evidence.
                keys = ('kind', 'authorized', 'input', 'sequence', 'expected', 'observed', 'required')
                if row['allegation'].get('kind') == 'behavioral':
                    keys = tuple(key for key in keys if key != 'required')
                elif row['allegation'].get('kind') == 'structural':
                    keys = ('kind', 'authorized', 'required', 'observed')
                entry['allegation'] = {key: row['allegation'][key] for key in keys if key in row['allegation']}
                for key, value in entry['allegation'].items():
                    if key != 'kind' and isinstance(value, str):
                        entry['allegation'][key] = value.strip()
                    elif key == 'sequence' and isinstance(value, list):
                        entry['allegation'][key] = [step.strip() if isinstance(step, str) else step for step in value]
        if exit_number is not None:
            entry['exit'] = exit_number
            if exit_lines is not None:
                entry['at'] = exit_lines[exit_number - 1].split(' -> ')[0]
        if 'allegation' not in entry and not any(_has_finding(entry.get(key, '')) for key in ('counterexample', 'not_shown')):
            issue(path, 'expected an actionable allegation or missing-code detail')
            return
        actions.append(entry)

    def negative(row):
        return (row.get('allegation') is not None or any(isinstance(row.get(key), str) and _has_finding(row[key])
                for key in ('counterexample', 'not_shown')) or row.get('assessment') == 'alleged')

    for position, row in enumerate(checks):
        path = f'checks[{position}]'
        if not isinstance(row, dict):
            issue(path, 'expected an object')
            continue
        n = _review_index(row.get('n'), requirements)
        if n is None:
            issue(path + '.n', f'expected an index in 1..{requirements}')
            continue
        assessment = row.get('assessment')
        if not indexed and assessment is None:
            assessment = ('alleged' if row.get('allegation') is not None or _has_finding(row.get('counterexample', ''))
                          else 'unseen' if _has_finding(row.get('not_shown', '')) else 'honored') if all(
                              isinstance(row.get(key, ''), str) for key in ('counterexample', 'not_shown')) else None
        if assessment not in ('honored', 'alleged', 'unseen'):
            issue(path + '.assessment', 'expected honored, alleged, or unseen')
        else:
            normalized = {'n': n, 'assessment': assessment}
            bind_source(row, n, normalized, path)
            if expected_sources is not None:
                if row.get('scope') != 'all_obligations':
                    issue(path + '.scope', 'expected all_obligations for the original approved source')
                else:
                    normalized['scope'] = 'all_obligations'
            if 'at' in row:
                if not isinstance(row['at'], str) or len(row['at']) > 300:
                    issue(path + '.at', 'expected a string of at most 300 characters')
                elif row['at'].strip():
                    normalized['at'] = row['at'].strip()
            if n in coverage:
                report['counts']['duplicate_checks'] += 1
                if coverage[n] != normalized:
                    conflicts.add(n)
                    issue(path, f'contradictory duplicate coverage for requirement {n}')
            else:
                coverage[n] = normalized
        permitted = {'n', 'assessment', 'at', 'source_id', 'scope'} if indexed else {'n', 'assessment', 'at', 'counterexample', 'not_shown', 'allegation'}
        if set(row) - permitted:
            issue(path, 'unexpected fields')
        if negative(row) and (not indexed or any(key in row for key in ('allegation', 'counterexample', 'not_shown'))):
            action(row, path)
        elif not any(key in row for key in ('assessment', 'counterexample', 'not_shown', 'allegation')):
            issue(path, 'missing check result')
        for key, limit in (('counterexample', 500), ('not_shown', 300)):
            if key in row and (not isinstance(row[key], str) or len(row[key]) > limit):
                issue(path + '.' + key, f'expected a string of at most {limit} characters')

    if indexed:
        missing = sorted(set(range(1, requirements + 1)) - set(coverage))
        if missing:
            issue('checks', f'missing {len(missing)} requirement indices; first indices {missing[:12]}')
        checked = raw.get('exits_checked')
        if not isinstance(checked, list):
            issue('exits_checked', 'expected a list')
            checked = []
        report['counts']['exit_rows'] = len(checked)
        known_exits = set()
        for position, value in enumerate(checked):
            e = _review_index(value, exit_count)
            if e is None:
                issue(f'exits_checked[{position}]', f'expected an index in 1..{exit_count}')
            else:
                known_exits.add(e)
        if len(known_exits) != exit_count:
            issue('exits_checked', f'missing {exit_count - len(known_exits)} listed exits')
        findings = raw.get('findings')
        if not isinstance(findings, list):
            issue('findings', 'expected a list')
            findings = []
        report['counts']['finding_rows'] = len(findings)
        for position, row in enumerate(findings):
            path = f'findings[{position}]'
            if isinstance(row, dict) and set(row) - {'n', 'exit', 'at', 'allegation', 'not_shown', 'source_id'}:
                issue(path, 'unexpected fields')
            action(row, path)
    else:
        judged = raw.get('exits', [])
        if not isinstance(judged, list):
            issue('exits', 'expected a list')
            judged = []
        report['counts']['exit_rows'] = len(judged)
        exit_claims = {}
        for position, row in enumerate(judged):
            path = f'exits[{position}]'
            if not isinstance(row, dict):
                issue(path, 'expected an object')
                continue
            e, n = _review_index(row.get('exit'), exit_count), _review_index(row.get('breaks'), requirements, zero=True)
            if e is None or n is None:
                issue(path, 'invalid exit index or requirement index')
                continue
            claims = exit_claims.setdefault(e, set())
            claims.add(n)
            if 0 in claims and len(claims) > 1:
                conflicts.update(claims - {0})
                issue(path, f'contradictory no-break and negative coverage for exit {e}')
            how = row.get('how')
            if not isinstance(how, str) or len(how) > 500:
                issue(path + '.how', 'expected a string of at most 500 characters')
                how = ''
            if set(row) - {'exit', 'breaks', 'how', 'allegation'}:
                issue(path, 'unexpected fields')
            if row.get('allegation') is not None:
                if not isinstance(row['allegation'], dict):
                    issue(path + '.allegation', 'expected an object or null')
                if n == 0:
                    issue(path + '.allegation', 'a no-break exit cannot carry an allegation')
            if n:
                action({'n': n, 'counterexample': how, **({'allegation': row['allegation']} if 'allegation' in row else {})}, path, e)

    distinct = {}
    for entry in actions:
        # An exit label is redundant once its exact location is resolved.
        identity = {key: value for key, value in entry.items() if key != 'exit' or exit_lines is None}
        if 'at' in identity:
            identity['at'] = _flat(identity['at'])
        if 'allegation' in identity:
            identity.pop('counterexample', None)
        key = json.dumps(identity, sort_keys=True, ensure_ascii=False)
        if key in distinct:
            report['counts']['duplicate_findings'] += 1
        else:
            distinct[key] = entry
    actions = list(distinct.values())
    report['counts']['distinct_findings'] = len(actions)
    if len(actions) > REVIEW_MAX_FINDINGS:
        issue('findings', f'{len(actions)} distinct actionable findings exceed the limit of {REVIEW_MAX_FINDINGS}')
    for entry in actions:
        n = entry['n']
        claim = 'alleged' if 'allegation' in entry or _has_finding(entry.get('counterexample', '')) else 'unseen'
        if n in coverage and coverage[n]['assessment'] != claim:
            conflicts.add(n)
            issue('findings', f'finding contradicts coverage for requirement {n}')
    if indexed:
        addressed = {entry['n'] for entry in actions}
        for n, row in coverage.items():
            if row['assessment'] == 'alleged' and n not in addressed:
                issue('findings', f'no actionable finding for alleged requirement {n}')
    report.update(checks=list(coverage.values()), findings=actions, conflicts=sorted(conflicts), complete=not issues)
    return report


def _counterexamples(reader, context: str, shown: str, code: str, seen: list[dict],
                     answer: str = '', sources=None) -> tuple[list[str], bool]:
    """Challenge positive readings and first-reader claims lacking evidence.

    A location alone is never a behavioral witness. Valid partial evidence is
    retained, but an ungrounded accusation can neither depart nor clear a row.
    """
    reviewed = [s for s in seen if s['state'] == 'ok' or s.get('allegation_status') == 'needs_witness']
    if not reviewed:
        return [], True
    listed = '\n'.join(f"{i}. " + (f"ORIGINAL SOURCE {s['source_id']} (whole verbatim text in APPROVED SOURCES)" if s.get('source_id')
                       else f"{s['needs']} ({'must not' if s['kind'] == 'must_not' else 'must'})")
                       + (f" at: {s['at']}" if s.get('at') else '')
                       + (' code checks: ' + json.dumps(s['code_checks'], ensure_ascii=False) if s.get('code_checks') else '')
                       + (' [first-reader allegation needs witness]' if s.get('allegation_status') else '')
                       for i, s in enumerate(reviewed, 1))
    exits = early_exits(shown)
    exits_text = ("\nEARLY EXITS THE DIFF SHOWS (added or unchanged):\n"
                  + "\n".join(f"{i}. {e}" for i, e in enumerate(exits, 1)) + "\n" if exits else "")
    system = COUNTEREXAMPLE_SYSTEM
    if _review_effort(reader, 'counterexample', True) is not None:
        system += COUNTEREXAMPLE_STOP_RULE
    try:
        raw = Client(reader).complete_json(
            'counterexample', system,
            context + f"\nREQUIREMENTS TO CHALLENGE:\n{listed}\n{exits_text}\nDIFF:\n{shown}",
            max_tokens=REVIEW_MAX_TOKENS, bounded=True)
    except LLMError:
        raw = None
    report = _counterexample_report(raw, len(reviewed), exits, sources=sources)
    complete = report['complete']
    unexamined = []
    if not complete:
        for item in reviewed:
            item['state'] = 'unclear'
            item['note'] = 'counterexample search did not complete'
        if not isinstance(raw, dict):
            return ['no search for counterexamples: the model did not answer'], False
        unexamined.append(_review_limitation(raw, 'the counterexample search returned malformed or incomplete data'))
        unexamined.extend(report['issues'])

    def unsupported(item, number):
        item['state'] = 'unclear'
        item['allegation_status'] = 'needs_witness'
        item['note'] = 'claimed departure lacks a grounded behavioral witness or explicit structural requirement'
        limitation = f"requirement {number}: the claimed departure lacks a grounded witness"
        if limitation not in unexamined:
            unexamined.append(limitation)

    def record(allegation, at):
        return {'what': clip_marked(_allegation_summary(allegation), 500, 'the witness ran on'),
                'at': at, 'located': True, 'allegation': allegation}

    def retain(item, value):
        item['state'] = 'unclear'
        item.pop('allegation_status', None)
        if not item.get('counterexample'):
            if not item.get('source_id'):
                item['needs'] = value['allegation']['authorized']
            item['counterexample'] = value
        elif value != item['counterexample']:
            alternatives = item.setdefault('counterexamples', [item['counterexample']])
            if value not in alternatives:
                alternatives.append(value)

    # Coverage never consumes the finding allowance. All coverage is examined
    # before retaining evidence, so a late conflicting status cannot erase it.
    addressed = {entry['n'] for entry in report['findings']}
    for check in report['checks']:
        n, assessment = check['n'], check['assessment']
        item = reviewed[n - 1]
        if assessment == 'unseen':
            item['state'] = 'unclear'
            unexamined.append(f'requirement {n}: the diff does not establish the behavior')
        elif assessment == 'alleged' and n not in addressed:
            unsupported(item, n)
        elif (complete and assessment == 'honored' and n not in addressed
              and located(check.get('at', ''), code) and item.get('allegation_status') == 'needs_witness'):
            # Rejecting an invented allegation does not make its invented
            # requirement true. Preserve the source-grounding guard.
            quoted = _flat(item['needs'])
            if (quoted and quoted in _flat(answer)
                    and len(quoted) >= min(12, len(_flat(answer)))):
                item.update(state='ok', found='honored', at=check['at'],
                            note='the critic rejected an unsupported first-reader allegation')
                item.pop('allegation_status', None)
            else:
                item['note'] = ('the critic rejected an unsupported allegation, but the requirement '
                                'it would mark honored was not quoted from the approval')

    candidates = []
    for entry in report['findings']:
        at = entry.get('at', '')
        source_answer = sources[entry['n'] - 1]['text'] if sources is not None else answer
        allegation = _grounded_allegation(entry.get('allegation'), source_answer, at, code, shown)
        candidates.append((entry, allegation))
    # Examine every candidate. Grounded evidence (including late conflicts)
    # has priority over unsupported claims and missing-code descriptions.
    candidates.sort(key=lambda pair: (pair[1] is None, pair[0]['n'] not in report['conflicts']))
    retained, omitted, evidence_chars = 0, 0, 0
    for entry, allegation in candidates:
        n = entry['n']
        item = reviewed[n - 1]
        value = record(allegation, entry.get('at', '')) if allegation is not None else None
        cost = len(json.dumps(value, ensure_ascii=False)) + 80 if value else 160
        # Reserve both primary and plural detail slots, including mixed findings.
        cost += 2 * len(entry.get('not_shown', '')) + 80 if entry.get('not_shown') else 0
        if value and item.get('counterexample') and not item.get('counterexamples'):
            cost += len(json.dumps(item['counterexample'], ensure_ascii=False)) + 40
        if retained >= REVIEW_MAX_FINDINGS or evidence_chars + cost > REVIEW_MAX_CHARS:
            omitted += 1
            item['state'] = 'unclear'
            if not item.get('counterexample'):
                item['note'] = 'additional actionable evidence was omitted by the bounded retention limit'
            continue
        retained += 1
        evidence_chars += cost
        if allegation is not None:
            retain(item, value)
        elif 'allegation' in entry or _has_finding(entry.get('counterexample', '')):
            if not (item.get('counterexample') or {}).get('allegation'):
                unsupported(item, n)
        not_shown = entry.get('not_shown', '').strip()
        if _has_finding(not_shown):
            item['state'] = 'unclear'
            if not item.get('not_shown'):
                item['not_shown'] = not_shown
            elif not_shown != item['not_shown']:
                details = item.setdefault('not_shown_details', [item['not_shown']])
                if not_shown not in details:
                    details.append(not_shown)
            if not_shown not in unexamined:
                unexamined.append(not_shown)
    if omitted:
        complete = False
        unexamined.append(f'{omitted} additional distinct actionable findings omitted; retained {retained} '
                          f'within the {REVIEW_MAX_FINDINGS}-finding/{REVIEW_MAX_CHARS}-character evidence limit')
    if not complete:
        for item in reviewed:
            if item['state'] == 'ok':
                item.update(state='unclear', note='counterexample search did not complete')

    for n, item in enumerate(reviewed, 1):
        if item.get('allegation_status') == 'needs_witness':
            limitation = f'requirement {n}: the first-reader allegation remains unsubstantiated'
            if limitation not in unexamined:
                unexamined.append(limitation)
    unexamined = list(dict.fromkeys(unexamined))
    # Overflow and contradictions must remain visible even after many malformed
    # rows; detailed parser diagnostics still retain all controlled reasons.
    unexamined.sort(key=lambda reason: not any(marker in reason for marker in
                    ('distinct actionable findings', 'contradict', 'additional distinct actionable findings omitted')))
    if len(unexamined) > 6:
        unexamined = unexamined[:5] + [f'{len(unexamined) - 5} additional limitations omitted from this summary; affected requirement states remain unclear']
    return unexamined, complete


def _shared_decisions(others):
    if not isinstance(others, (tuple, list)):
        raise ValueError('shared decisions must be a sequence')
    records = []
    for pair in others:
        if (not isinstance(pair, (tuple, list)) or len(pair) != 2
                or any(not isinstance(text, str) for text in pair)):
            raise ValueError('shared decisions need exact question and answer strings')
        question, answer = pair
        if answer.strip():
            records.append({'question': question, 'answer': answer})
    return records


def _other_decisions(others) -> str:
    """Preserve shared authority exactly; the caller bounds the whole context."""
    records = _shared_decisions(others)
    return ("\nOTHER DECISIONS ON THIS TASK, also authorized (not requirements of this one):\n"
            + json.dumps(records, ensure_ascii=False) + "\n" if records else "")



MEMORY_RERANK_SYSTEM = (
    "You are shown one question a coding agent needs decided, and a few decisions the organization answered "
    "before that a cheap text score thought were close but not close enough. Say whether any ONE of them "
    "already answers the new question. It does only if acting on that earlier answer would be acting on what "
    "the new question asks: the same decision about the same thing, where the differences between them do not "
    "change what the answer would be. A decision about a different column, a different endpoint, a different "
    "customer, a different release or a different component is NOT the same decision, however similar the "
    "words. Neither is an answer that merely bears on the question or sets a general direction. A stated "
    "rule is different from a direction: an earlier answer that sets a rule for a whole class of things, in "
    "so many words (every numeric option, all public endpoints), answers a question about one member of that "
    "class that it does not exclude, and the why says it is that rule applied. Measured live: a signed rule "
    "for every numeric Retry option was read as not answering the same question about a new numeric option, "
    "and its owner was asked to confirm their own policy again. Pick nothing "
    "whenever you are unsure, which will be most of the time: picking nothing costs one question to a person, "
    "and picking wrongly puts an answer nobody gave in front of an agent about to act on it. Return ONLY "
    "JSON: {\"id\": the id of the one decision that answers it, or \"none\", \"why\": one sentence naming "
    "what makes it the same decision, or what differs}. No em dashes or en dashes."
)


def rerank_memories(cfg, question: str, candidates: list[dict]) -> tuple[str, str]:
    """Which of the near-miss earlier answers actually answers this, as
    (decision id, why). ("", "") when there is no backend, the model
    fails, or none of them does.

    The cheap score decides what is worth reading; this decides what is
    worth reusing. It only ever runs where the deterministic floor found
    nothing, so it can add a candidate and never take one away, and what
    it adds still has to survive the checks a deterministic hit does."""
    if not cfg or not cfg.semantic_retrieval or not candidates:
        return "", ""
    known = {str(c["id"]): c for c in candidates}
    lines = []
    for c in candidates:
        lines.append(f"[{c['id']}] scored {c.get('score', 0):.2f}\n  asked: {str(c.get('question', ''))[:300]}\n"
                     f"  answered: {str(c.get('answer', ''))[:400]}")
    try:
        raw = Client(cfg.fast()).complete_json(
            "memory_rerank", MEMORY_RERANK_SYSTEM,
            f"NEW QUESTION: {question}\n\nEARLIER DECISIONS:\n" + "\n".join(lines), max_tokens=400)
    except LLMError:
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    picked = str(raw.get("id", "")).strip()
    # Closed set: it selects among what it was shown or it selects nothing.
    if picked not in known:
        return "", ""
    return picked, str(raw.get("why", "")).strip()[:300]


READ_REPLY_SYSTEM = (
    "You read one message a person sent in reply to a decision they were asked about, and say what they "
    "meant by it. You are given the decision, the answer already on the table if there is one, and their "
    "message. Decide which of these it is: \"answer\" (they state what should be done), \"signoff\" (they "
    "agree with the answer on the table and add nothing), \"handoff\" (this is not theirs and they name or "
    "point at somebody else), \"rule\" (they say this should hold in future cases too), \"question\" (they "
    "ask something back before deciding), or \"chat\" (anything else, including thinking aloud, partial "
    "opinions and acknowledgements). Prefer \"chat\" whenever you are unsure: a message wrongly read as an "
    "answer becomes a decision somebody is recorded as having made. A bare acknowledgement is always "
    "\"chat\" and never \"signoff\": \"ok\", \"sure\", \"thanks\", \"got it\", \"noted\", \"seen\" and the "
    "like say the person read the message, not that they agree with it. \"signoff\" needs them to say the "
    "answer is right. A statement of what is usually or normally done (\"we usually ship these off by "
    "default\", \"that is how we have always done it\", \"the convention here is X\") is \"chat\" even when "
    "it happens to match the answer on the table: it says what tends to happen, not that this decision is "
    "settled, and a person who meant to settle it will say so. For \"answer\", put what they decided "
    "in `answer`, in their own words, as a statement of the decision and not a paraphrase of the question; "
    "put their reason in `rationale` if they gave one, otherwise leave it empty. For \"handoff\", put who "
    "they named in `to`, exactly as they wrote it. Add nothing they did not say, never resolve the decision "
    "yourself, and never treat a question of theirs as an answer. Return ONLY JSON: {\"kind\": \"...\", "
    "\"answer\": \"...\", \"rationale\": \"...\", \"to\": \"...\", \"confident\": true or false}. No em "
    "dashes or en dashes."
)

READ_REPLY_KINDS = ("answer", "signoff", "handoff", "rule", "question", "chat")


def read_reply(cfg, question: str, on_table: str, text: str) -> dict:
    """What a person's free-text reply meant, or an empty dict when there
    is no backend, the model fails, or it is not confident.

    This never applies anything. Raven reads the message back to the
    person and applies it when they confirm, because a message wrongly
    read as an answer becomes a decision somebody is recorded as having
    made, and that is the one mistake the whole contract exists to
    prevent."""
    if not cfg or not cfg.semantic_retrieval:
        return {}
    prompt = (f"DECISION: {question}\nANSWER ON THE TABLE: {(on_table or '').strip()[:600] or 'none yet'}\n"
              f"THEIR MESSAGE: {(text or '').strip()[:1500]}")
    try:
        raw = Client(cfg.fast()).complete_json("read_reply", READ_REPLY_SYSTEM, prompt, max_tokens=400)
    except LLMError:
        return {}
    if not isinstance(raw, dict):
        return {}
    kind = str(raw.get("kind", "")).strip().lower()
    if kind not in READ_REPLY_KINDS or not raw.get("confident"):
        return {}
    answer = str(raw.get("answer", "")).strip()
    rationale = str(raw.get("rationale", "")).strip()
    if kind == "answer" and (not answer or looks_meta(answer)):
        return {}
    # The person confirms this reading as their answer: it is never cut to
    # fit (it was cut at 700 characters). A reading longer than anything a
    # reply restates is not a reading to offer.
    if len(answer) > 3000 or len(rationale) > 3000:
        return {}
    return {"kind": kind, "answer": answer, "rationale": rationale, "to": str(raw.get("to", "")).strip()[:120]}


ASSUMABLE_CATEGORIES = {"rollout", "compat", "ux"}

_ASSUMED_DEFAULTS = {
    "rollout": "Ship behind a feature flag, default off, until someone says "
               "otherwise.",
    "compat": "Preserve existing behavior for current users; only new "
              "usage gets the new path.",
    "ux": "Match the closest existing surface rather than inventing a new "
          "pattern.",
}
