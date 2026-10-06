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
REVIEW_MAX_FINDINGS = 4


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
        thinks = self.cfg.model in _THINKS
        payload = self._messages(system, prompt, max_tokens + (THINKING_ROOM if thinks else 0))
        if not thinks and _cut_short_by_thinking(payload):
            # This model thinks before it answers, and the thinking counts
            # against max_tokens. Measured live on the conformance read: a
            # 1200-token budget went to thinking on two of six decisions,
            # no text came back, and the reads were silently missing. Every
            # call to it from now on gets room to think; this one is asked
            # again with that room.
            _THINKS.add(self.cfg.model)
            thinks = True
            payload = self._messages(system, prompt, max_tokens + THINKING_ROOM)
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
            payload = self._messages(system, prompt, 2 * max_tokens + (THINKING_ROOM if thinks else 0))
            if payload.get("stop_reason") == "max_tokens":
                raise LLMError(f"the model's {purpose} answer was cut off at max_tokens twice; not used")
        return _anthropic_text(payload)

    def _messages(self, system: str, prompt: str, max_tokens: int) -> dict:
        headers = {"x-api-key": self.cfg.api_key, "anthropic-version": "2023-06-01"}
        workspace = self.cfg.anthropic_workspace_id
        if workspace:
            if len(workspace) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in workspace):
                raise LLMError("ANTHROPIC_WORKSPACE_ID must be a printable header value of at most 256 characters")
            headers["anthropic-workspace-id"] = workspace
        payload = self._post_json(
            "https://api.anthropic.com/v1/messages",
            headers,
            {"model": self.cfg.model, "max_tokens": max_tokens,
             "system": system,
             "messages": [{"role": "user", "content": prompt}]})
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


CONFORMANCE_SYSTEM = (
    "You are given one decision a person authorized, in their words, and a diff. Work in two steps and do "
    "not skip the first. " + TEMPORAL_REVIEW_GUIDANCE + REVIEW_OUTPUT_GUIDANCE +
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
    "you saw and it will be worked out from that. Return ONLY JSON: {\"status\": \"complete\" or \"inconclusive\", "
    "\"unexamined\": \"\" or a short limitation, \"requirements\": [{\"needs\": \"...\", "
    "\"kind\": \"must\" or \"must_not\", \"at\": \"the line\", \"found\": \"honored\" or \"missing\" or "
    "\"violated\" or \"unseen\"}], \"why\": one sentence naming what is missing or where you read it}. No em "
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
    "authorized, in their words, the diff, and numbered requirements a first reader said the diff honors, "
    "each with the line it pointed at. " + TEMPORAL_REVIEW_GUIDANCE + REVIEW_OUTPUT_GUIDANCE +
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
    "requirements have no counterexample: omit their entries rather than stretch. Inspect ALL numbered "
    "requirements and listed exits, but return at most four checks and four exits, with at most one concrete "
    "counterexample per requirement. Do not repeat a check in exits. Omit empty/no-finding entries entirely; "
    "status complete explicitly attests that the omitted requirements and exits were examined too. If more "
    "findings exist than fit, preserve the strongest concrete findings and use inconclusive. Return ONLY JSON: "
    "{\"status\": \"complete\" or \"inconclusive\", \"unexamined\": \"\" or a short limitation, "
    "\"checks\": [{\"n\": the requirement's number, \"counterexample\": \"\" or one sentence, \"at\": the line "
    "of the diff it goes through, copied exactly, \"not_shown\": \"\" or the code it depends on that the diff "
    "does not show}], \"exits\": [{\"exit\": the exit's number, \"breaks\": the requirement's number or 0, "
    "\"how\": \"\" or one sentence: the call, the value, and what then happens}]}. No em dashes or en dashes."
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
    REVIEW_OUTPUT_GUIDANCE +
    "Read the authorized decisions and the diff. Find additional policy conditions or exemptions "
    "the change introduces which none of those decisions authorizes. Look for extra customer, plan, "
    "region, account or feature-flag branches that change who gets the behavior. Do not invent "
    "requirements, treat ordinary validation as policy, or report conditions that the other signed "
    "decisions allow. Quote an exact line of production code for each finding. Return ONLY JSON: "
    '{"status": "complete" or "inconclusive", "unexamined": "" or a short limitation, '
    '"conditions": [{"condition": "the extra condition and its effect", "at": "exact code line"}]}. '
    "Return at most four conditions; if more exist, retain concrete findings and use inconclusive. "
    "Return an empty conditions list when there is no such addition. Test expectations alone are not evidence."
)


def _unstated_conditions(reader, context: str, shown: str, code: str, seen: list[dict]) -> tuple[list[str], bool]:
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
        found = located(at, code)
        seen.append({'needs': what, 'kind': 'must_not', 'found': 'violated' if found else 'unseen',
                     'state': 'departs' if found else 'unclear', 'at': at,
                     'note': 'the diff adds a condition no signed answer states' if found else
                             'the extra condition was not located in the change'})
        if not found:
            unexamined.append(what)
    return unexamined, complete


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
    context = f"DECISION: {question}\nWHAT WAS AUTHORIZED: {answer}\n" + _other_decisions(others)
    try:
        raw = Client(reader).complete_json("conformance", CONFORMANCE_SYSTEM, context + f"\nDIFF:\n{shown}",
                                           max_tokens=REVIEW_MAX_TOKENS, bounded=True)
    except LLMError:
        raw = None
    raw_reqs = raw.get('requirements') if isinstance(raw, dict) else None
    reqs = [r for r in raw_reqs if _requirement_shape(r)] if isinstance(raw_reqs, list) else []
    first_complete = (_review_complete(raw, 'requirements') and bool(reqs)
                      and len(reqs) == len(raw_reqs))
    if not reqs:
        return _inconclusive_review('the requirement reading did not complete')
    # The model reports what it saw; the verdict is worked out here. Told
    # to judge directly it blessed a change that dropped two of the three
    # values an owner signed for, and told to weigh its own observations
    # it marked "do not clamp in the application" unseen and went
    # uncertain on a change that plainly did what was asked.
    seen = []
    for r in reqs:
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
            named_calls = set(re.findall(r'\b([A-Za-z_]\w*)\(\)', str(r.get("needs", ""))))
            absent = sorted(name for name in named_calls
                            if not re.search(r'\b' + re.escape(name) + r'\s*\(', production))
            if absent:
                # A real host's increment() delegated to new(), but its unchanged
                # body was not in the diff. The reader invented a missing params
                # update there. Tests mentioning the function are not its body.
                state = "unclear"
                note = "missing behavior was claimed in code the diff does not show: " + ', '.join(name + '()' for name in absent)
        if state == "ok" and found in ("honored", "present") and not located(at, kept) \
                and not (_STRUCTURAL_RE.search(str(r.get("needs", ""))) and located_header(at, shown)):
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
        item = {"needs": clip_marked(str(r.get("needs", "")), 400, "the requirement ran on"),
                "kind": "must_not" if forbids else "must", "found": found, "state": state}
        if at.strip():
            item["at"] = at
        if note:
            item["note"] = note
        seen.append(item)
    unexamined = []
    if not first_complete:
        limitation = _review_limitation(raw, 'the requirement reading did not complete')
        unexamined.append(limitation)
        seen.append({'needs': 'Every signed requirement examined', 'kind': 'must', 'found': 'unseen',
                     'state': 'unclear', 'note': limitation})
    if len(diff) > DIFF_READ:
        cut = _unread_files(diff, DIFF_READ)
        unexamined.append(f"the diff past its first {DIFF_READ} characters ({len(diff)} given)"
                          + (f": {', '.join(cut[:8])}" + (f" and {len(cut) - 8} more" if len(cut) > 8 else "")
                             if cut else ""))
    complete = first_complete
    if any(item['state'] == 'ok' for item in seen):
        limitations, stage_complete = _counterexamples(reader, context, shown, diff_kept(shown, tests=False), seen)
        unexamined += limitations
        complete = complete and stage_complete
    if any(item['state'] == 'ok' for item in seen):
        limitations, stage_complete = _unstated_conditions(reader, context, shown, diff_kept(shown, tests=False), seen)
        unexamined += limitations
        complete = complete and stage_complete
    states = {s["state"] for s in seen}
    verdict = "departs" if "departs" in states else ("unclear" if "unclear" in states or unexamined else "follows")
    # Whole, or cut at a word and marked. Measured live on a397f1c: five of
    # seven reasons ended mid-word at 300 characters ("used by sleep_for_r").
    why = str(raw.get("why", "")).strip()
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
        named = [s["needs"] for s in seen if s["state"] == state and s["needs"]]
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
    additions = [item['needs'] for item in seen if item.get('note') == 'the diff adds a condition no signed answer states']
    if additions:
        why = 'The diff adds a condition no signed answer states: ' + '; '.join(additions)
    if verdict == 'unclear' and unexamined and not countered:
        why = 'The review is inconclusive: ' + '; '.join(unexamined[:2]) + '. ' + why
    why = clip_marked(why, 1200, "the requirements list what was read")
    out = {"verdict": verdict, "why": why, "requirements": seen}
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


def _inconclusive_review(reason):
    return {'verdict': 'unclear', 'why': reason + '; the advisory reading is inconclusive.',
            'requirements': [], 'unexamined': [reason], 'incomplete': True}


def _requirement_shape(row):
    return (isinstance(row, dict) and isinstance(row.get('needs'), str) and bool(row['needs'].strip())
            and row.get('kind') in ('must', 'must_not')
            and row.get('found') in ('honored', 'missing', 'violated', 'unseen', 'present')
            and isinstance(row.get('at', ''), str))


def _condition_shape(row):
    return (isinstance(row, dict) and isinstance(row.get('condition'), str) and bool(row['condition'].strip())
            and isinstance(row.get('at'), str))


def _has_finding(value):
    return bool(value.strip()) and value.strip().lower().rstrip('.') not in ('none', 'no', 'n/a')


def _counterexample_shape(raw, requirements, exits):
    """Malformed model output cannot establish a successful adversarial read."""
    if not isinstance(raw, dict) or not isinstance(raw.get('checks'), list):
        return False
    if 'exits' in raw and not isinstance(raw['exits'], list):
        return False
    for check in raw['checks']:
        if not isinstance(check, dict):
            return False
        try:
            number = int(str(check.get('n', '')).strip().rstrip('.'))
        except ValueError:
            return False
        if not 1 <= number <= requirements or not any(key in check for key in ('counterexample', 'not_shown')):
            return False
        if any(key in check and (not isinstance(check[key], str) or len(check[key]) > limit)
               for key, limit in (('counterexample', 500), ('at', 300), ('not_shown', 300))):
            return False
    for check in raw.get('exits', []):
        if (not isinstance(check, dict) or 'breaks' not in check or not isinstance(check.get('how'), str)
                or len(check['how']) > 500):
            return False
        try:
            position = int(str(check.get('exit', '')).strip())
            number = int(str(check.get('breaks', '0')).strip() or 0)
        except ValueError:
            return False
        if not 1 <= position <= exits or not 0 <= number <= requirements:
            return False
    return True


def _counterexamples(reader, context: str, shown: str, code: str, seen: list[dict]) -> tuple[list[str], bool]:
    """Try to break each requirement read as met, in place: one with a
    counterexample through a line of the change's own code stops reading
    as met and carries it. One whose line is not there (a paraphrase, a
    test, code the diff does not show) is kept beside the requirement and
    listed as unexamined; uncertainty cannot support an all-clear. Returns
    the unexamined scope and whether the structured search completed."""
    met = [s for s in seen if s["state"] == "ok"]
    if not met:
        return [], True
    listed = "\n".join(f"{i}. {s['needs']} ({'must not' if s['kind'] == 'must_not' else 'must'})"
                       + (f" at: {s['at']}" if s.get("at") else "") for i, s in enumerate(met, 1))
    exits = early_exits(shown)
    exits_text = ("\nEARLY EXITS THE DIFF SHOWS (added or unchanged):\n"
                  + "\n".join(f"{i}. {e}" for i, e in enumerate(exits, 1)) + "\n" if exits else "")
    try:
        raw = Client(reader).complete_json(
            "counterexample", COUNTEREXAMPLE_SYSTEM,
            context + f"\nREQUIREMENTS READ AS HONORED:\n{listed}\n{exits_text}\nDIFF:\n{shown}",
            max_tokens=REVIEW_MAX_TOKENS, bounded=True)
    except LLMError:
        raw = None
    unexamined = []
    complete = (_review_complete(raw, 'checks') and _counterexample_shape(raw, len(met), len(exits))
                and len(raw['checks']) <= REVIEW_MAX_FINDINGS
                and len(raw.get('exits', [])) <= REVIEW_MAX_FINDINGS)
    if not complete:
        # A failed or malformed second reading cannot support a "follows"
        # verdict. Keep independently valid counterexamples from a partial
        # reply, rather than throwing away a concrete finding with bad data.
        for item in met:
            item["state"] = "unclear"
            item["note"] = "counterexample search did not complete"
        if not isinstance(raw, dict):
            return ["no search for counterexamples: the model did not answer"], False
        unexamined.append(_review_limitation(raw, "the counterexample search returned malformed or incomplete data"))
        checks = raw.get('checks') if isinstance(raw.get('checks'), list) else []
        judged_exits = raw.get('exits') if isinstance(raw.get('exits'), list) else []
        raw = {
            'checks': [check for check in checks
                       if _counterexample_shape({'checks': [check]}, len(met), len(exits))],
            'exits': [check for check in judged_exits
                      if _counterexample_shape({'checks': [], 'exits': [check]}, len(met), len(exits))],
        }
    # Empty legacy entries are not findings and must not crowd a concrete
    # counterexample out of a partial/oversized response.
    findings = [check for check in raw.get('checks', [])
                if any(_has_finding(check.get(field, '')) for field in ('counterexample', 'not_shown'))]
    for check in findings[:REVIEW_MAX_FINDINGS]:
        if not isinstance(check, dict):
            continue
        try:
            n = int(str(check.get("n", "")).strip().rstrip("."))
        except ValueError:
            continue
        if not 1 <= n <= len(met):
            continue
        item = met[n - 1]
        what = clip_marked(str(check.get("counterexample") or ""), 500, "the counterexample ran on")
        if _has_finding(what):
            at = clip_marked(str(check.get("at") or ""), 300, "the line ran on")
            found = located(at, code) or bool(_STRUCTURAL_RE.search(item["needs"]) and located_header(at, shown))
            if found or not (item.get('counterexample') or {}).get('located'):
                item["counterexample"] = {"what": what, "at": at, "located": found}
            item["state"] = "unclear"
            if not found:
                # Measured live on the conformance panel: a path through a
                # branch the diff does not show, pinned to a test's name.
                unexamined.append(f"a possible counterexample whose line is not in the change's code: {what}")
        not_shown = clip_marked(str(check.get("not_shown") or ""), 300, "it ran on")
        if _has_finding(not_shown):
            item["state"] = "unclear"
            item["not_shown"] = not_shown
            if not_shown not in unexamined:
                unexamined.append(not_shown)
    # Each early exit judged on its own. Measured live on the eb9d22d
    # patch: asked for counterexamples in general, the search named the
    # zero-backoff return in one run of four.
    exit_findings = [check for check in raw.get('exits', []) if _has_finding(check.get('how', ''))]
    for judged in exit_findings[:REVIEW_MAX_FINDINGS]:
        if not isinstance(judged, dict):
            continue
        try:
            e, n = int(str(judged.get("exit", "")).strip()), int(str(judged.get("breaks", "0")).strip() or 0)
        except ValueError:
            continue
        how = clip_marked(str(judged.get("how") or ""), 500, "the counterexample ran on")
        if not (1 <= e <= len(exits) and 1 <= n <= len(met) and how):
            continue
        item = met[n - 1]
        if (item.get("counterexample") or {}).get("located"):
            continue
        # The exit came from the diff, so its line is there by construction.
        item["state"] = "unclear"
        item["counterexample"] = {"what": how, "at": exits[e - 1].split(" -> ")[0], "located": True}
    return unexamined[:6], complete


def _other_decisions(others) -> str:
    """The task's other authorized decisions, for the conformance read.
    Measured live: three signed answers on one task, each read alone, and
    the one about negative values called the decimal parsing another
    answer had authorized "not authorized"."""
    lines = [f"- {q.strip()[:200]}: {a.strip()[:300]}" for q, a in list(others)[:6] if (a or "").strip()]
    return ("\nOTHER DECISIONS ON THIS TASK, also authorized (not requirements of this one):\n" + "\n".join(lines) + "\n"
            if lines else "")


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
