"""People as git sees them: a name, an email, sometimes a handle. The
same person appears as "Daniel P. Berrangé" in a trailer, "Daniel
Berrange" in a MAINTAINERS entry, and @berrange in a CODEOWNERS line, so
matching is by email first, then by accent-folded name, then by first
and last token, then by handle."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_QUOTE_RE = re.compile(r"[\"'`]")
_MACHINE_RE = re.compile(
    r"\[bot\]$|^dependabot|^renovate|^github$|^github[- ]actions|^snyk-bot|^greenkeeper"
    r"|^imgbot|^pre-commit-ci|^allcontributors|^semantic-release|^release-please"
    r"|^netlify|^vercel$|github bot$|^copybara|^bors$|^k8s-ci-robot|^openshift-merge"
    r"|^nixpkgs-ci|^r-ryantm|^nixpkgs-merge-bot|^backportbot", re.IGNORECASE)


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


def is_machine(name: str) -> bool:
    return bool(_MACHINE_RE.search((name or "").strip()))


@dataclass(frozen=True)
class Person:
    name: str = ""
    email: str = ""
    handle: str = ""

    @property
    def key(self) -> str:
        return self.email.lower() or norm_name(self.name) or self.handle.lower()

    @property
    def label(self) -> str:
        return self.name or (f"@{self.handle}" if self.handle else self.email)

    def matches(self, other: "Person") -> bool:
        return same_person(self, other)


def same_person(a: Person, b: Person) -> bool:
    if a.email and b.email and a.email.lower() == b.email.lower():
        return True
    if a.handle and b.handle and a.handle.lower() == b.handle.lower():
        return True
    na, nb = norm_name(a.name), norm_name(b.name)
    if na and nb:
        if na == nb:
            return True
        ta, tb = name_tokens(a.name), name_tokens(b.name)
        if len(ta) >= 2 and len(tb) >= 2 and ta[0] == tb[0] and ta[-1] == tb[-1]:
            return True
        # An initial-only middle name ("Daniel P. Berrange" vs "Daniel Berrange")
        # is handled by the first/last rule; a mononym is compared whole.
    if a.handle and nb and norm_name(a.handle) == nb.replace(" ", ""):
        return True
    if b.handle and na and norm_name(b.handle) == na.replace(" ", ""):
        return True
    return False


def name_matches(name: str, people: list[Person]) -> Person | None:
    """The labeled person a routed display name refers to, if any."""
    probe = parse_person(name)
    for p in people:
        if same_person(probe, p):
            return p
    return None


_ADDR_RE = re.compile(r"^\s*\"?([^\"<]*?)\"?\s*(?:<([^>]+)>)?\s*$")


def parse_person(text: str) -> Person:
    """'Full Name <email>' or '@handle' or a bare name."""
    text = (text or "").strip()
    if text.startswith("@") and " " not in text:
        return Person(handle=text[1:])
    m = _ADDR_RE.match(text)
    if not m:
        return Person(name=text)
    name, email = (m.group(1) or "").strip(), (m.group(2) or "").strip()
    if not name and email:
        return Person(email=email)
    if "@" in name and not email:
        return Person(email=name)
    return Person(name=name, email=email)


@dataclass
class Directory:
    """Handle to person resolution learned from the repository itself:
    noreply emails (12345+handle@users.noreply.github.com), a README
    collaborator list, or nixpkgs' maintainer-list.nix."""
    by_handle: dict[str, Person] = field(default_factory=dict)

    def add(self, handle: str, person: Person) -> None:
        h = handle.lower().lstrip("@")
        if h and h not in self.by_handle:
            self.by_handle[h] = Person(person.name, person.email, h)

    def resolve(self, handle: str) -> Person:
        h = handle.lower().lstrip("@")
        return self.by_handle.get(h, Person(handle=h))

    def learn_email(self, name: str, email: str) -> None:
        m = re.match(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$", email or "")
        if m:
            self.add(m.group(1), Person(name=name, email=email))


_README_ROW = re.compile(r"\*\s+\[([A-Za-z0-9_-]+)\]\(https://github\.com/[^)]+\)\s*-\s*\*\*([^*]+)\*\*\s*(?:<<([^>]+)>>)?")


def learn_readme(directory: Directory, text: str) -> int:
    n = 0
    for handle, name, email in _README_ROW.findall(text or ""):
        directory.add(handle, Person(name=name.strip(), email=(email or "").strip()))
        n += 1
    return n


_NIX_ENTRY = re.compile(r"\n\s*([A-Za-z0-9_-]+)\s*=\s*\{(.*?)\n\s*\};", re.DOTALL)
_NIX_FIELD = re.compile(r"\b(email|github|name)\s*=\s*\"([^\"]*)\"")


def learn_nix_maintainers(directory: Directory, text: str) -> int:
    n = 0
    for _attr, body in _NIX_ENTRY.findall(text or ""):
        fields = dict(_NIX_FIELD.findall(body))
        handle = fields.get("github", "")
        if handle:
            directory.add(handle, Person(name=fields.get("name", ""), email=fields.get("email", "")))
            n += 1
    return n
