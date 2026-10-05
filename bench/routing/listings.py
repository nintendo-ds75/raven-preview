"""Static ownership listings as of a revision: the kernel-style MAINTAINERS
file (QEMU and Linux) and GitHub CODEOWNERS (nixpkgs keeps one at
ci/OWNERS). Both are read from the object store at the replay revision, so
nothing after T leaks in."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from .identity import Directory, Person, parse_person


def git_show(repo: str, rev: str, rel: str) -> str:
    out = subprocess.run(["git", "-C", repo, "show", f"{rev}:{rel}"], capture_output=True,
                         text=True, timeout=120)
    return out.stdout if out.returncode == 0 else ""


# ---------------- MAINTAINERS ----------------

@dataclass
class Section:
    title: str
    maintainers: list[Person] = field(default_factory=list)
    reviewers: list[Person] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    name_res: list[str] = field(default_factory=list)


def _f_pattern_re(pattern: str) -> re.Pattern:
    """A MAINTAINERS F: pattern as a regex over a repository path: a
    trailing slash means everything below, * does not cross a slash, a
    bare path is exact."""
    pat = pattern.strip()
    if pat.endswith("/"):
        body = re.escape(pat).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
        return re.compile("^" + body + ".*$")
    body = re.escape(pat).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.compile("^" + body + "$")


def parse_maintainers(text: str) -> list[Section]:
    sections: list[Section] = []
    cur: Section | None = None
    pending_title: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            if cur is not None and (cur.files or cur.maintainers or cur.reviewers):
                sections.append(cur)
            cur, pending_title = None, []
            continue
        m = re.match(r"^([A-Z]):\s*(.*)$", line)
        if m and len(line) > 2 and line[1] == ":":
            if cur is None:
                cur = Section(title=" ".join(pending_title).strip())
            tag, value = m.group(1), m.group(2).strip()
            if tag == "M":
                cur.maintainers.append(parse_person(value))
            elif tag == "R":
                cur.reviewers.append(parse_person(value))
            elif tag == "F":
                cur.files.append(value)
            elif tag == "X":
                cur.excludes.append(value)
            elif tag == "N":
                cur.name_res.append(value)
            continue
        if cur is None:
            if not set(line.strip()) <= {"-", "="}:
                pending_title.append(line.strip())
        # A non-tag line inside a section is a title continuation; ignore.
    if cur is not None and (cur.files or cur.maintainers or cur.reviewers):
        sections.append(cur)
    return sections


class Maintainers:
    def __init__(self, text: str):
        self.sections = parse_maintainers(text)
        self._compiled = []
        for s in self.sections:
            files = [_f_pattern_re(p) for p in s.files]
            excl = [_f_pattern_re(p) for p in s.excludes]
            names = []
            for n in s.name_res:
                try:
                    names.append(re.compile(n))
                except re.error:
                    pass
            self._compiled.append((s, files, excl, names))

    def sections_for(self, path: str) -> list[tuple[Section, str]]:
        """Sections whose F: (or N:) patterns cover the path, with the
        matched pattern, most specific (longest pattern) first. The
        catch-all sections (F: * and F: */) are marked by their pattern."""
        out = []
        for s, files, excl, names in self._compiled:
            if any(x.match(path) for x in excl):
                continue
            hit = ""
            for pat, rx in zip(s.files, files):
                if rx.match(path):
                    if len(pat) > len(hit):
                        hit = pat
            if not hit:
                for n, rx in zip(s.name_res, names):
                    if rx.search(path):
                        hit = f"N:{n}"
                        break
            if hit:
                out.append((s, hit))
        out.sort(key=lambda x: -len(x[1].rstrip("/")) if not x[1].startswith("N:") else 0)
        return out

    def people_for(self, paths: list[str]) -> tuple[list[Person], list[str]]:
        """Maintainers and designated reviewers listed for any of the
        paths, plus the patterns that matched. The project-wide catch-all
        (F: * or F: */ or a single-segment pattern of only wildcards) is
        left out."""
        people: list[Person] = []
        patterns: list[str] = []
        seen = set()
        for path in paths:
            for s, pat in self.sections_for(path):
                if _is_catch_all(pat):
                    continue
                if pat not in patterns:
                    patterns.append(pat)
                for p in s.maintainers + s.reviewers:
                    if p.key not in seen:
                        seen.add(p.key)
                        people.append(p)
        return people, patterns


def _is_catch_all(pattern: str) -> bool:
    core = pattern.strip("/").replace("*", "")
    return core == "" or pattern.startswith("N:")


# ---------------- CODEOWNERS ----------------

def _codeowners_re(pattern: str) -> re.Pattern:
    """gitignore-style CODEOWNERS pattern to a regex over a repo path."""
    pat = pattern.strip()
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    parts = []
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**", i):
            if pat.startswith("**/", i):
                parts.append("(?:.*/)?")
                i += 3
                continue
            parts.append(".*")
            i += 2
            continue
        if c == "*":
            parts.append("[^/]*")
        elif c == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(c))
        i += 1
    body = "".join(parts)
    prefix = "^" if (anchored or "/" in pat) else "^(?:.*/)?"
    suffix = "(?:/.*)?$" if not dir_only else "/.*$"
    if pat.endswith("*") and not dir_only:
        suffix = "(?:/.*)?$"
    return re.compile(prefix + body + suffix)


class Codeowners:
    def __init__(self, text: str, directory: Directory | None = None):
        self.rules: list[tuple[str, list[str], re.Pattern]] = []
        self.directory = directory or Directory()
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            pattern = parts[0]
            owners = [o.lstrip("@") for o in parts[1:] if o.startswith("@") or "@" in o]
            try:
                self.rules.append((pattern, owners, _codeowners_re(pattern)))
            except re.error:
                continue

    def owners_for(self, path: str) -> tuple[list[str], str]:
        """Last matching rule wins, as GitHub applies it."""
        for pattern, owners, rx in reversed(self.rules):
            if rx.match(path):
                return owners, pattern
        return [], ""

    def people_for(self, paths: list[str]) -> tuple[list[Person], list[str], list[str]]:
        """(humans, team handles, matched patterns) for the paths."""
        humans: list[Person] = []
        teams: list[str] = []
        patterns: list[str] = []
        seen = set()
        for path in paths:
            owners, pat = self.owners_for(path)
            if pat and pat not in patterns:
                patterns.append(pat)
            for o in owners:
                if "/" in o:
                    if o not in teams:
                        teams.append(o)
                    continue
                p = self.directory.resolve(o) if "@" not in o else Person(email=o)
                if p.key not in seen:
                    seen.add(p.key)
                    humans.append(p)
        return humans, teams, patterns
