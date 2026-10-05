"""People as the organization knows them, not as git spells them.

One person appears as "Daniel P. Berrangé" in a trailer, "Daniel
Berrange" in a MAINTAINERS entry, @berrange in CODEOWNERS, a GitHub
login on a review, a Slack member id in a thread, and "Daniel" when a
teammate types the requester's name. Matching is by email first, then
by GitHub login or handle, then by accent-folded full name, then by
first and last token, then by an alias the person table records, and
last by a first name that is unique among the people the graph knows.

The people table (bridge/graph.py) is the source of truth for verified
identities; the engineers git history produced are candidates for
linking, never verified on their own.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_QUOTE_RE = re.compile(r"[\"'`]")
_ADDR_RE = re.compile(r"^\s*\"?([^\"<]*?)\"?\s*(?:<([^>]+)>)?\s*$")
_NOREPLY_RE = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$", re.IGNORECASE)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.lower()


def norm_name(name: str) -> str:
    name = _PAREN_RE.sub("", name or "")
    name = _QUOTE_RE.sub("", name)
    name = name.replace(",", " ")
    return " ".join(fold(name).split())


def name_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s.\-]+", norm_name(name)) if t]


def norm_handle(handle: str) -> str:
    return (handle or "").strip().lstrip("@").lower()


@dataclass(frozen=True)
class Person:
    """One identity as far as the caller knows it. Any field may be
    empty; `id` is set when the person table knows them."""
    name: str = ""
    email: str = ""
    handle: str = ""      # a GitHub login or a listing handle
    id: str = ""
    slack_id: str = ""
    aliases: tuple = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return self.email.lower() or norm_name(self.name) or norm_handle(self.handle)

    @property
    def label(self) -> str:
        return self.name or (f"@{self.handle}" if self.handle else self.email)

    def matches(self, other: "Person") -> bool:
        return same_person(self, other)


def parse_person(text: str) -> Person:
    """'Full Name <email>', 'email', '@handle', or a bare name."""
    text = (text or "").strip()
    if not text:
        return Person()
    if text.startswith("@") and " " not in text:
        return Person(handle=text[1:])
    m = _ADDR_RE.match(text)
    if not m:
        return Person(name=text)
    name, email = (m.group(1) or "").strip(), (m.group(2) or "").strip()
    if not name and email:
        return Person(email=email)
    if "@" in name and " " not in name and not email:
        return Person(email=name)
    return Person(name=name, email=email)


def same_person(a: Person, b: Person) -> bool:
    """Two identities name one person: the same email, the same handle,
    the same folded name, the same first and last token, or a handle
    that is the other's name run together. A noreply GitHub email names
    its login."""
    ea, eb = a.email.lower(), b.email.lower()
    if ea and eb and ea == eb:
        return True
    ha, hb = norm_handle(a.handle), norm_handle(b.handle)
    for e, h in ((ea, hb), (eb, ha)):
        m = _NOREPLY_RE.match(e or "")
        if m and h and m.group(1).lower() == h:
            return True
    if ha and hb and ha == hb:
        return True
    na, nb = norm_name(a.name), norm_name(b.name)
    if na and nb:
        if na == nb:
            return True
        ta, tb = name_tokens(a.name), name_tokens(b.name)
        if len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0] and ta[-1] == tb[-1]:
            return True
    if ha and nb and ha == nb.replace(" ", ""):
        return True
    if hb and na and hb == na.replace(" ", ""):
        return True
    for alias in a.aliases:
        if same_person(parse_person(alias), Person(name=b.name, email=b.email, handle=b.handle)):
            return True
    for alias in b.aliases:
        if same_person(Person(name=a.name, email=a.email, handle=a.handle), parse_person(alias)):
            return True
    return False


def name_matches(name: str, people: list[Person]) -> Person | None:
    """The person a display name, email or handle refers to, if any."""
    probe = parse_person(name)
    for p in people:
        if same_person(probe, p):
            return p
    return None


def first_name_match(text: str, candidates: list[str]) -> str:
    """A bare first name that exactly one known full name starts with
    ('Priya' among 'Priya Natarajan' and 'Yuki Tanaka') resolves to that
    full name; anything ambiguous or unknown stays as given."""
    probe = norm_name(text)
    if not probe or " " in probe:
        return text
    hits = [c for c in candidates if name_tokens(c) and name_tokens(c)[0] == probe or norm_name(c).replace(" ", "") == probe]
    return hits[0] if len(hits) == 1 else text
