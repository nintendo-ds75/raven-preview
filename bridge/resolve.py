"""From a question to the paths it is about.

Most real questions name no file. The resolver tries, in order of
confidence: the path the agent is working in; path-like tokens in the
question (hw/riscv/virt.c, drm/amd/display, bufferiszero.c), matched to
the tree exactly or by suffix; the question's own words against the
tree's directory and file names with rarity weighting, including joined
neighbours (buffer is zero -> bufferiszero, af xdp -> af-xdp); the paths
of indexed records on the same subject; and the paths of earlier
decisions. Every hit carries the reason it was chosen, which becomes
part of the evidence line."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .graph import Graph

MAX_HITS = 4
TERM_FLOOR = 3.0            # idf a matched tree token must reach (df at most N/20)
UBIQUITOUS_SHARE = 0.05     # a component in more than this share of paths names nothing
_STOP = {"should", "would", "could", "approve", "approv", "change", "changes", "this", "that", "with", "from",
         "when", "what", "who", "which", "make", "made", "use", "used", "using", "add", "added", "adding",
         "remove", "removed", "fix", "fixed", "fixes", "allow", "into", "onto", "than", "then", "them",
         "their", "there", "these", "those", "have", "has", "had", "does", "not", "and", "the", "for",
         "our", "your", "you", "are", "was", "were", "will", "can", "also", "only", "just", "more", "less",
         "new", "old", "all", "any", "some", "same", "after", "before", "over", "under", "via", "per",
         "off", "out", "set", "get", "let", "put", "run", "yes", "now", "how", "why", "where", "while",
         "each", "both", "such", "very", "much", "many", "most", "own", "way", "one", "two", "instead",
         "still", "already", "again", "again", "always", "never", "here", "case", "cases", "support",
         "supported", "enable", "enabled", "disable", "disabled", "update", "updated", "cleanup", "clean",
         "avoid", "handle", "handling", "check", "checks", "missing", "unused", "correct", "wrong", "error",
         "errors", "warning", "warnings", "code", "file", "files", "function", "functions", "call", "calls",
         "value", "values", "default", "defaults", "option", "options", "type", "types", "name", "names",
         "size", "sizes", "version", "versions", "return", "returns", "drop", "move", "rename", "convert",
         "introduce", "implement", "simplify", "refactor", "revert", "bump", "init", "initial", "path",
         "paths", "directory", "test", "tests", "testing", "doc", "docs", "src", "lib", "include", "common",
         "core", "util", "utils", "misc", "main", "base", "config", "data", "build", "tools", "scripts"}

VENDORED = {"deps", "vendor", "third_party", "thirdparty", "node_modules", "external", "3rdparty", "subprojects"}

_PATHISH_RE = re.compile(r"(?<![\w/])([A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+*-]+)+)/?")
_FILENAME_RE = re.compile(
    r"(?<![\w/])([A-Za-z0-9_+-]{2,}\.(?:c|h|cc|cpp|hpp|py|rs|js|mjs|cjs|ts|tsx|md|rst|json|ya?ml|toml|go|sh"
    r"|nix|dts|dtsi|inc|txt|html|css|sql|pl|java|kt|rb|php|cs|swift|m|mm|el|decode|build))(?![\w/])")


_NORM_RE = re.compile(r"[^a-z0-9]")
_PIECE_RE = re.compile(r"[_\-.]")


def _norm(component: str) -> str:
    return _NORM_RE.sub("", component.lower())


def _stem_of(basename: str) -> str:
    return basename.rsplit(".", 1)[0] if "." in basename and not basename.startswith(".") else basename


@dataclass
class PathHit:
    path: str
    weight: float
    why: str

    @property
    def is_dir(self) -> bool:
        return self.path.endswith("/")


class Tree:
    """The tree at the source's revision, indexed by normalized
    component so words can be matched to paths cheaply."""

    def __init__(self, paths: list[str]):
        # Every bucket below is built in path order, and the sets are only
        # ever walked sorted, so what a question resolves to does not
        # depend on the process's hash seed.
        paths = sorted(paths)
        self.files = set(paths)
        self.dirs: set[str] = set()
        # Files by their base name and by that name without its extension:
        # the only files a suffix like virt.c or riscv/virt can end with.
        self.files_by_base: dict[str, list[str]] = {}
        self.by_token: dict[str, set[str]] = {}
        # (token, path) -> the directory whose name the token matched, when
        # it matched a directory component rather than the file's name.
        self.dir_of_token: dict[tuple[str, str], str] = {}
        # (token, path) pairs where the token is only a piece of a component.
        self.piece: set[tuple[str, str]] = set()
        for p in paths:
            parts = p.split("/")
            for k in range(1, len(parts)):
                self.dirs.add("/".join(parts[:k]) + "/")
            self.files_by_base.setdefault(parts[-1], []).append(p)
            if "." in parts[-1]:
                self.files_by_base.setdefault(parts[-1].rsplit(".", 1)[0], []).append(p)
            seen: set[str] = set()
            for i, comp in enumerate(parts):
                last = i == len(parts) - 1
                base = _stem_of(comp) if last else comp
                toks = [_norm(base)]
                # node_sqlite.cc is also found by "sqlite"; xlnx-versal-canfd.c by "canfd".
                pieces = [x for x in _PIECE_RE.split(base) if len(x) >= 3]
                piece_toks = [_norm(x) for x in pieces] if len(pieces) > 1 else []
                for tok in toks + piece_toks:
                    if tok and tok not in seen:
                        seen.add(tok)
                        self.by_token.setdefault(tok, set()).add(p)
                        if tok in piece_toks and tok not in toks:
                            self.piece.add((tok, p))
                        if not last:
                            self.dir_of_token.setdefault((tok, p), "/".join(parts[:i + 1]) + "/")
        self.n = max(1, len(paths))
        self.dir_by_base: dict[str, list[str]] = {}
        for d in sorted(self.dirs):
            self.dir_by_base.setdefault(_norm(d.rstrip("/").rsplit("/", 1)[-1]), []).append(d)

    @property
    def floor(self) -> float:
        """The idf a matched token must reach to count at all."""
        return min(TERM_FLOOR, max(0.3, math.log(self.n / 4)))

    @property
    def strong(self) -> float:
        """The idf of a token that names an area outright: one found in
        at most five paths of the tree."""
        return max(self.floor, math.log(self.n / 5))

    def idf(self, tok: str) -> float:
        df = len(self.by_token.get(tok, ()))
        if not df or df > max(3, self.n * UBIQUITOUS_SHARE):
            return 0.0
        return math.log(self.n / df)


def tree_for(store: Graph, repo: str) -> Tree:
    return store.memo(repo, "tree", (), lambda: store.artifact_paths(repo), Tree)


def _dir_of(path: str) -> str:
    return path.rsplit("/", 1)[0] + "/" if "/" in path else ""


def _add(hits: dict[str, PathHit], path: str, weight: float, why: str) -> None:
    cur = hits.get(path)
    if cur is None or weight > cur.weight:
        hits[path] = PathHit(path, weight, why)


def _suffix_matches(tree: Tree, token: str) -> tuple[list[str], list[str]]:
    """Files and directories whose path ends with the token (as whole
    components), the token given with or without an extension."""
    t = token.strip("/").rstrip(":,.;")
    if not t:
        return [], []
    files = []
    if t in tree.files:
        files.append(t)
    low = "/" + t
    for f in tree.files_by_base.get(t.rsplit("/", 1)[-1], ()):
        if f == t or f.endswith(low):
            files.append(f)
        elif "." in f.rsplit("/", 1)[-1]:
            stem = f.rsplit(".", 1)[0]
            if stem == t or stem.endswith(low):
                files.append(f)
    dirs = [d for d in tree.dirs if d.rstrip("/") == t or d.rstrip("/").endswith(low)]
    return sorted(set(files))[:6], sorted(set(dirs))[:6]


_DOTTED_RE = re.compile(r"(?<![\w/.@])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?![\w/])")
_MODULE_EXTS = (".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".rb", ".go", ".rs", ".java", ".kt", ".cs", ".php",
                ".swift", ".scala", ".ex", ".exs")


def module_hits(tree: Tree, text: str) -> dict[str, PathHit]:
    """A dotted module or symbol path (billing.usage.invoice_cents,
    acme.billing.rates) names the file or package it lives in. Measured
    live: a task that named `billing.usage.invoice_cents` resolved only
    to the data file it also mentioned, and the module's author was
    never a candidate."""
    hits: dict[str, PathHit] = {}
    for raw in _DOTTED_RE.findall(text):
        parts = raw.split(".")
        if _FILENAME_RE.fullmatch(raw) or len(parts) < 2:
            continue
        for k in range(len(parts), 1, -1):
            base = "/".join(parts[:k])
            file = next((base + ext for ext in _MODULE_EXTS if base + ext in tree.files), "")
            if file:
                _add(hits, file, 0.95, f"the question names {raw}, which is {file}")
                break
            if base + "/" in tree.dirs:
                _add(hits, base + "/", 0.9, f"the question names {raw}, which is {base}/")
                break
    return hits


def explicit_hits(tree: Tree, text: str) -> dict[str, PathHit]:
    hits: dict[str, PathHit] = dict(module_hits(tree, text))
    tokens = [m.group(1) for m in _PATHISH_RE.finditer(text)]
    tokens += [m.group(1) for m in _FILENAME_RE.finditer(text)]
    for raw in tokens:
        if raw.lower().startswith(("http", "https", "git@")) or raw.count("/") > 8:
            continue
        tok = raw.strip("/").rstrip(":,.;)")
        if not tok or re.fullmatch(r"[\d./]+", tok):
            continue
        files, dirs = _suffix_matches(tree, tok)
        exact_file = tok in tree.files
        exact_dir = tok + "/" in tree.dirs
        if exact_file:
            _add(hits, tok, 1.0, f"the question names {tok}")
            continue
        if exact_dir:
            _add(hits, tok + "/", 1.0, f"the question names {tok}/")
            continue
        if len(files) == 1:
            _add(hits, files[0], 0.95, f"the question names {tok}, which is {files[0]}")
            continue
        if 1 <= len(dirs) <= 2 and not files:
            for d in dirs:
                _add(hits, d, 0.9, f"the question names {tok}, which is {d}")
            continue
        if files and len(files) <= 4:
            # A bare name matching a source and its header names the source.
            ranked = sorted(files, key=lambda f: (f.startswith("include/") or f.endswith((".h", ".hpp")), f))
            for i, f in enumerate(ranked):
                _add(hits, f, 0.9 if i == 0 else 0.6, f"the question names {tok}, matched to {f}")
            continue
        if dirs and len(dirs) <= 4:
            for d in dirs:
                _add(hits, d, 0.65, f"the question names {tok}, matched to {d}")
            continue
        # A path whose head exists even if the tail does not (a renamed
        # or new file): keep the deepest known directory.
        parts = tok.split("/")
        found = False
        for k in range(len(parts) - 1, 0, -1):
            cand = "/".join(parts[:k]) + "/"
            if cand in tree.dirs:
                _add(hits, cand, 0.55 + 0.05 * k, f"the question names {tok}; {cand} is in the tree")
                found = True
                break
        if found:
            continue
        # An area written the way people say it (drm/amdgpu/mes11 for
        # drivers/gpu/drm/amd/amdgpu/): the longest run of its components
        # that names at most a few directories.
        for length in range(len(parts) - 1, 0, -1):
            for i in range(0, len(parts) - length + 1):
                chain = "/".join(parts[i:i + length])
                if len(chain) < 4:
                    continue
                dirs = [d for d in tree.dirs if d.rstrip("/").endswith("/" + chain) or d.rstrip("/") == chain]
                dirs = [d for d in dirs if d.split("/", 1)[0] not in VENDORED] or dirs
                if 1 <= len(dirs) <= 3:
                    for d in sorted(dirs):
                        _add(hits, d, 0.8 if len(dirs) == 1 else 0.6,
                             f"the question names {tok}; {chain} is {d}")
                    found = True
                    break
            if found:
                break
    return hits


def _question_tokens(text: str) -> list[tuple[str, bool]]:
    """(token, name_like): a token is name-like when it came from an
    identifier (digits, inner capitals, underscores, hyphens, dots), so
    a plain English word that happens to be a filename does not count
    on its own."""
    words = re.findall(r"[A-Za-z0-9_+.-]+", text)
    out: list[tuple[str, bool]] = []
    for w in words:
        w = w.strip(".")
        if not w:
            continue
        parts = [p for p in re.split(r"[_\-+.]", w) if p]
        namey = bool(re.search(r"\d|_|-|\.|[a-z][A-Z]", w)) or w.isupper() and len(w) > 2
        low = w.lower()
        if len(parts) > 1:
            out.append((_norm(low), True))
            # A piece of an identifier (open from qemu_open_old) is a plain
            # word unless the piece itself looks like a name.
            out.extend((p.lower(), bool(re.search(r"\d|[a-z][A-Z]", p)) or p.isupper() and len(p) > 2) for p in parts)
        else:
            out.append((low, namey))
    return out


def _whole_words(text: str) -> list[str]:
    """Words that stand alone in the text, not pieces of identifiers."""
    return [w.lower() for w in re.findall(r"(?<![\w_.-])[A-Za-z][A-Za-z0-9]*(?![\w_.-])", text)]


def term_hits(tree: Tree, text: str, common: set[str] | None = None) -> dict[str, PathHit]:
    """Words of the question against directory and file names, rarity
    weighted, with joined neighbours so identifiers split by spaces or
    underscores still find their file. A single plain word that happens
    to be a filename (memory, rings, commit) never names an area on its
    own: a lone match must be name-like, or a rare word that the
    repository's own records do not use as ordinary language."""
    toks = _question_tokens(text)
    common = common or set()
    cands: dict[str, tuple[float, bool]] = {}
    for i, (t, namey) in enumerate(toks):
        variants = {(t, namey)}
        if i + 1 < len(toks):
            variants.add((t + toks[i + 1][0], True))
        if i + 2 < len(toks):
            variants.add((t + toks[i + 1][0] + toks[i + 2][0], True))
        for v, vn in variants:
            v = _norm(v)
            if len(v) < 3 or v in _STOP or v.isdigit():
                continue
            idf = tree.idf(v)
            if idf < tree.floor:
                continue
            if len(v) == 3 and len(tree.by_token.get(v, ())) > (10 if vn else 3):
                continue
            if v not in cands or idf > cands[v][0]:
                cands[v] = (idf, vn or cands.get(v, (0, False))[1])
    if not cands:
        return {}
    strong = tree.strong
    # Tests and docs mention every subsystem's names, and vendored trees
    # carry a whole other project's names; they are the area only when
    # the question says so.
    about_tests = any(t in ("test", "tests", "testing", "qtest", "iotests", "avocado", "fixtures") for t, _ in toks)
    about_docs = any(t in ("doc", "docs", "documentation", "manual") for t, _ in toks)
    about_deps = any(t in ("deps", "dep", "vendor", "vendored", "third_party", "thirdparty", "upgrade", "bump",
                           "update", "node_modules") for t, _ in toks)
    scored: dict[str, tuple[float, list[str]]] = {}
    for tok, (idf, _) in cands.items():
        for p in sorted(tree.by_token[tok]):
            top = p.split("/", 1)[0]
            scale = 1.0
            if not about_deps and (top in VENDORED or "/node_modules/" in p or "/third_party/" in p or "/vendor/" in p):
                scale = 0.35
            elif not about_tests and (top in ("tests", "test", "testsuite", "t") or "/tests/" in p or "/test/" in p):
                scale = 0.5
            elif not about_docs and top in ("docs", "doc", "Documentation"):
                scale = 0.5
            sc, used = scored.get(p, (0.0, []))
            scored[p] = (sc + idf * scale, used + [tok])
    # A token that named the file (its stem or a piece of it) credits the
    # file itself; one that named a directory credits that directory, so
    # "http2" finds src/node_http2.cc and lib/internal/http2/, never all
    # of src/. Several files under one directory matching the same rare
    # word is a strong area signal.
    dirs: dict[str, tuple[float, set[str]]] = {}
    for p, (sc, used) in scored.items():
        if len(used) == 1:
            t = used[0]
            if (t, p) in tree.piece and not cands[t][1]:
                continue
        named_file = any((t, p) not in tree.dir_of_token for t in used)
        if named_file:
            d = p
        else:
            d = max((tree.dir_of_token[(t, p)] for t in used), key=len)
        ds, dt = dirs.get(d, (0.0, set()))
        dirs[d] = (max(ds, sc), dt | set(used))

    def lone_ok(tok: str) -> bool:
        idf, namey = cands[tok]
        if namey:
            return len(tok) >= 4
        return len(tok) >= 5 and tok not in common and idf >= strong

    hits: dict[str, PathHit] = {}
    ranked = sorted(dirs.items(), key=lambda kv: (-kv[1][0], kv[0]))
    for d, (sc, used) in ranked[:4]:
        if sc < tree.floor:
            continue
        if len(used) == 1 and not lone_ok(next(iter(used))):
            continue
        w = 0.8 * min(1.0, sc / strong)
        namey_used = sum(1 for t in used if cands[t][1])
        if len(used) >= 2:
            # Two identifiers name an area firmly; two plain words less so.
            w = min(0.9 if namey_used else 0.75, w + 0.1)
        elif not cands[next(iter(used))][1]:
            w = min(w, 0.35)
        why = "matched " + ", ".join(sorted(used)[:3]) + f" to {d}"
        _add(hits, d, w, why)
    # The single best file, when a rare word names it outright.
    fp, (fs, fused) = max(scored.items(), key=lambda kv: (kv[1][0], -len(kv[0])))
    best = max(sc for sc, _ in scored.values())
    if fs >= best and fs >= tree.floor and _norm(_stem_of(fp.rsplit("/", 1)[-1])) in fused \
            and (len(fused) >= 2 or lone_ok(next(iter(fused)))):
        _add(hits, fp, min(0.9, 0.8 * min(1.0, fs / strong) + 0.05), f"matched {', '.join(sorted(fused)[:3])} to {fp}")
    return hits


def root_file_hits(tree: Tree, text: str, common: set[str] | None = None) -> dict[str, PathHit]:
    """A word that is exactly a root-level file's stem (meson, configure,
    Makefile) names that file even though the stem is common elsewhere,
    unless the repository's own records use the word as plain language."""
    hits: dict[str, PathHit] = {}
    roots = {_norm(_stem_of(f)): f for f in sorted(tree.files) if "/" not in f}
    for tok, _ in _question_tokens(text):
        f = roots.get(_norm(tok))
        if f and len(tok) >= 4 and tok.lower() not in _STOP and tok.lower() not in (common or set()):
            _add(hits, f, 0.85, f"the question names {f}")
    return hits


_AREA_RE = re.compile(r"^(?:[^:?]{0,60}:\s*)?Should\s+(?:the\s+)?([A-Za-z0-9_.+/-]+)", re.IGNORECASE)


def area_word_hits(tree: Tree, text: str) -> dict[str, PathHit]:
    """The subject of a 'Should X ...' question is its area even when X
    is an ordinary word (doc, test, src, util): a directory of that name
    first, else the module files named after it (lib/sqlite.js,
    src/node_sqlite.cc, doc/api/sqlite.md), each at its own scope."""
    m = _AREA_RE.match(text.strip())
    if not m:
        return {}
    word = m.group(1).strip(".,;:").lower()
    if word in ("we", "it", "this", "that", "they", "you", "bridge", "there", "all") or len(word) < 3 or "/" in word:
        return {}
    hits: dict[str, PathHit] = {}
    key = _norm(word)
    vendored_ok = word in VENDORED
    dirs = [d for d in tree.dir_by_base.get(key, [])
            if (vendored_ok or d.split("/", 1)[0] not in VENDORED) and not d.startswith(".")]
    aux = re.compile(r"^(tests?|docs?|Documentation)/")
    source_dirs = dirs if aux.match(word + "/") else [d for d in dirs if not aux.match(d)]
    top = [d for d in (source_dirs or dirs) if d.count("/") == 1]
    chosen = top or source_dirs or dirs
    for d in chosen[:3]:
        w = 0.85 if len(chosen) == 1 else 0.6
        if not source_dirs and dirs:
            w = 0.5
        _add(hits, d, w, f"the question is about {d}")
    if top or len(word) < 4:
        return hits
    files = []
    for f in sorted(tree.by_token.get(key, ())):
        if (not vendored_ok and f.split("/", 1)[0] in VENDORED) or f.startswith("."):
            continue
        stem = _stem_of(f.rsplit("/", 1)[-1])
        # sqlite names lib/sqlite.js and src/node_sqlite.cc (a piece after a
        # separator), never test_msa_and_v.c for "and".
        pieces = [_norm(x) for x in re.split(r"[_\-.]", stem)]
        if _norm(stem) == key or (len(key) >= 5 and word not in _STOP and key in pieces):
            files.append(f)
    files = [f for f in files if not re.match(r"^(tests?|docs?|Documentation)/", f)] or files
    if 0 < len(files) <= 6:
        for f in sorted(files):
            _add(hits, f, 0.6 if hits else 0.7, f"the question is about {word}, which is {f}")
    return hits


def dir_name_hits(tree: Tree, text: str) -> dict[str, PathHit]:
    """A plain word that is exactly a directory's name (accel, migration,
    gdbstub) names that directory: one directory of that name is a firm
    area, a few are candidates, many is no signal."""
    hits: dict[str, PathHit] = {}
    words = _whole_words(text)
    about_tests = any(w in ("test", "tests", "testing") for w in words)
    for low in words:
        if len(low) < 4 or low in _STOP:
            continue
        dirs = [d for d in tree.dir_by_base.get(_norm(low), []) if d.split("/", 1)[0] not in VENDORED]
        if not about_tests:
            dirs = [d for d in dirs if not re.match(r"^(tests?|docs?|Documentation)/", d) and "/tests/" not in d]
        if not dirs or len(dirs) > 3:
            continue
        # A top-level directory of that name outranks nested ones.
        ordered = sorted(dirs, key=lambda d: (d.count("/"), d))
        for i, d in enumerate(ordered):
            w = 0.7 if len(dirs) == 1 else (0.6 if i == 0 and ordered[0].count("/") == 1 else 0.45)
            _add(hits, d, w, f"the question names the directory {d}")
    return hits


def common_words(store: Graph, repo: str, tokens: list[str]) -> set[str]:
    """Tokens the repository's own records use as ordinary language: a
    word in more than 2% of the indexed record texts."""
    if not tokens:
        return set()
    n = store.db.execute("SELECT count(*) c FROM intents WHERE repo=?", (repo,)).fetchone()["c"]
    if n < 20:
        return set()
    dfs = store.term_dfs(sorted(set(tokens)), repo=repo)
    return {t for t, df in dfs.items() if df / n > 0.02}


def record_hits(store: Graph, repo: str, text: str) -> dict[str, PathHit]:
    """Paths of indexed records about the same subject. The subject is
    carried by rare terms: a record shares the subject when it covers
    two of the question's rarest terms, at least one of them a name
    (an identifier, or a word few records use), and the record itself is
    about one area (few directories), not a tree-wide merge."""
    from .llm import stem
    raw = [(w, stem(w.lower())) for w in re.findall(r"[A-Za-z0-9_]{3,}", text)
           if w.lower() not in _STOP]
    terms = [t for _, t in raw if t not in _STOP and len(t) >= 4]
    namey = {t for w, t in raw if re.search(r"\d|_|[a-z][A-Z]", w) and len(t) >= 4}
    if len(terms) < 2:
        return {}
    n = store.db.execute("SELECT count(*) c FROM intents WHERE repo=?", (repo,)).fetchone()["c"]
    if n < 5:
        return {}
    dfs = store.term_dfs(sorted(set(terms)), repo=repo)
    rare = {t for t in terms if 0 < dfs.get(t, 0) <= max(2, 0.01 * n)}
    focus = sorted((t for t in terms if 0 < dfs.get(t, 0)), key=lambda t: dfs[t])[:4]
    if len(focus) < 2 or not (rare | namey) & set(focus):
        return {}
    hits: dict[str, PathHit] = {}
    rows = store.intents_matching(terms, limit=6, repo=repo)
    paths = store.paths_of_intents(repo, [(r["kind"], r["ref"]) for r in rows])
    for r in rows:
        blob = (r["title"] + " " + r["body"]).lower()
        covered = [t for t in focus if t in blob]
        # Two rare words, or one identifier, carry a subject; two ordinary
        # words that happen to recur do not.
        if len(covered) < 2 or not (sum(1 for t in covered if t in rare) >= 2 or any(t in namey for t in covered)):
            continue
        plist = paths.get((r["kind"], r["ref"]), [])
        dirs = {_dir_of(p) or p for p in plist}
        if not plist or len(dirs) > 4:
            continue
        for d in sorted(dirs):
            _add(hits, d, 0.5, f"record {r['kind']} {r['ref']} ({', '.join(covered[:2])}) touched {d}")
    return hits


def memory_hits(store: Graph, repo: str, text: str) -> dict[str, PathHit]:
    hits: dict[str, PathHit] = {}
    try:
        items = store.memory_search(text, limit=3, repo=repo, min_score=0.5)
    except Exception:
        return {}
    files = tree_for(store, repo).files
    for it in items:
        p = (it.get("path") or "").strip()
        if not p or p == "unknown":
            continue
        _add(hits, p if p in files else _dir_of(p) or p, 0.5,
             f"earlier decision {it['id']} on the same subject was about {p}")
    return hits


def resolve_paths(store: Graph, repo: str, question: str, context: str = "", path: str = "",
                  hints: list[str] | None = None, also_paths: list[str] | None = None) -> list[PathHit]:
    """The paths a question is about, best first. `path` and `also_paths`
    are where the agent is working (explicit hits, every one of them: a
    node on "usage.json,billing/usage.py" was once judged under
    usage.json alone, where nobody held a clear share, while the person
    who wrote all of billing/usage.py was never looked at). `hints` are
    paths the question inherits from the decision above it or from its
    task: they count only when the question itself names no clear area."""
    tree = tree_for(store, repo)
    hits: dict[str, PathHit] = {}
    for named in [path, *(also_paths or [])]:
        if not named or named == "unknown":
            continue
        p = named.lstrip("/")
        if p in tree.files:
            _add(hits, p, 1.0, f"the agent is working in {p}")
        elif p.rstrip("/") + "/" in tree.dirs:
            _add(hits, p.rstrip("/") + "/", 1.0, f"the agent is working in {p}")
        else:
            d = _dir_of(p)
            if d in tree.dirs:
                _add(hits, d, 0.8, f"the agent is working in {p}; {d} is in the tree")
    def best() -> float:
        return max((h.weight for h in hits.values()), default=0.0)

    if tree.files:
        for k, v in explicit_hits(tree, question).items():
            _add(hits, k, v.weight, v.why)
        common = common_words(store, repo, [t for t, _ in _question_tokens(question + " " + (context or ""))])
        # A question word that happens to be a root file's stem ("license"
        # and LICENSE.BSD) is a guess at the area; once the agent named a
        # path that exists, it stays a secondary hit, so a file's sole
        # author cannot outrank the people who own the code being changed.
        # Legal boilerplate (LICENSE, COPYING, NOTICE, AUTHORS) is never the
        # area then: who wrote the licence text says nothing about who
        # decides code that handles licences.
        anchored = best() >= 0.8
        for k, v in root_file_hits(tree, question, common).items():
            if anchored and _norm(_stem_of(k)) in LEGAL_STEMS:
                continue
            _add(hits, k, v.weight * (ROOT_WORD_WHEN_ANCHORED if anchored else 1.0),
                 v.why + ("; secondary to the path the agent named" if anchored else ""))
        if best() < 0.9:
            for k, v in area_word_hits(tree, question).items():
                _add(hits, k, v.weight, v.why)
            for k, v in dir_name_hits(tree, question).items():
                _add(hits, k, v.weight, v.why)
            for k, v in term_hits(tree, question, common).items():
                _add(hits, k, v.weight, v.why)
            if context and best() < 0.6:
                for k, v in explicit_hits(tree, context).items():
                    _add(hits, k, v.weight * 0.7, v.why + " (from the context)")
                for k, v in term_hits(tree, context, common).items():
                    # A lone word from the body is never an area; two are.
                    if len(v.why.split("matched ", 1)[-1].split(" to ")[0].split(", ")) >= 2:
                        _add(hits, k, v.weight * 0.7, v.why + " (from the context)")
    if best() < 0.6:
        for k, v in record_hits(store, repo, question).items():
            _add(hits, k, v.weight, v.why)
        for k, v in memory_hits(store, repo, question).items():
            _add(hits, k, v.weight, v.why)
    # Inherited paths count only when the question names no area of its
    # own worth the name; they never outrank the question's own words.
    if hints and best() < INHERITED_BELOW:
        for h in hints[:4]:
            p = h.strip().lstrip("/")
            if not p:
                continue
            if p in tree.files:
                _add(hits, p, INHERITED_WEIGHT, f"inherited: the decision above or the task is about {p}")
            elif p.rstrip("/") + "/" in tree.dirs:
                _add(hits, p.rstrip("/") + "/", INHERITED_WEIGHT,
                     f"inherited: the decision above or the task is about {p.rstrip('/')}/")
            elif not tree.files:
                # No tree to check it against, as for a path the agent
                # names: the authority map still reads it. Measured: a node
                # in a repository Raven had not indexed reached the
                # reviewer of its task's file but not the person who
                # decides it.
                _add(hits, p, INHERITED_WEIGHT, f"inherited: the decision above or the task is about {p}")
    if best() < 0.8:
        for k, v in model_area_hits(store, repo, tree, question, context).items():
            _add(hits, k, v.weight, v.why)
    out = sorted(hits.values(), key=lambda h: (-h.weight, h.path))
    return out[:MAX_HITS]


INHERITED_WEIGHT = 0.8
ROOT_WORD_WHEN_ANCHORED = 0.4
LEGAL_STEMS = frozenset({"license", "licence", "copying", "notice", "copyright", "authors", "contributors"})
INHERITED_BELOW = 0.6
MODEL_AREA_WEIGHT = 0.75
MODEL_AREA_MAX_ENTRIES = 600

AREA_SYSTEM = (
    "You map a question about a code change to the places in a repository it is about. You are given the "
    "question, optional context, and a list of the repository's directories and top-level files. Return ONLY JSON: "
    "{\"paths\": [up to 3 entries copied exactly from the list, most specific first], \"confidence\": \"high\" or "
    "\"low\", \"why\": one short sentence}. Pick only entries that appear in the list, never invent one. Prefer the "
    "directory that holds the code the question would change over documentation or tests about it. Confidence is "
    "high only when the question names a subsystem, device, file, function, format or concept that clearly lives in "
    "those entries; a vague question (a bare verb, generic words like fix, remove, table, context) is low, and a "
    "low-confidence answer is treated as no answer. If nothing maps, return {\"paths\": [], \"confidence\": \"low\", "
    "\"why\": \"...\"}."
)
MODEL_AREA_MIN_TERMS = 3


def _area_candidates(tree: Tree) -> list[str]:
    """Directories to depth three and the top-level files, the way a
    person skims a tree: enough to name an area, small enough to read."""
    depth: dict[str, int] = {}
    for d in tree.dirs:
        depth[d] = d.count("/")
    dirs = sorted((d for d, k in depth.items() if k <= 3), key=lambda d: (depth[d], d))
    files = sorted(f for f in tree.files if "/" not in f)
    out = dirs[:MODEL_AREA_MAX_ENTRIES] + files[:60]
    return out


def model_area_hits(store: Graph, repo: str, tree: Tree, question: str, context: str) -> dict[str, PathHit]:
    """With a model key present, a fast model reads the question and the
    tree's directories and names the area; cached per question. Off
    without a backend or with BRIDGE_SEMANTIC=0."""
    from .config import load
    cfg = load()
    if not cfg.semantic_retrieval or not tree.files:
        return {}
    # A question with almost no words of its own is not worth a guess.
    words = [t for t, plain in _question_tokens(question) if t not in _STOP and len(t) >= 3]
    if len(words) < MODEL_AREA_MIN_TERMS:
        return {}
    import hashlib
    import json as _json
    from . import llm as llm_mod
    key = hashlib.sha256((question.strip() + "\n" + (context or "").strip()[:800]).encode()).hexdigest()[:24]
    cached = store.cache_get(repo, "model_area", key)
    if cached is None:
        candidates = _area_candidates(tree)
        prompt = (f"QUESTION: {question.strip()}\n" + (f"CONTEXT: {context.strip()[:800]}\n" if context else "")
                  + "REPOSITORY ENTRIES:\n" + "\n".join(candidates))
        try:
            raw = llm_mod.Client(cfg.fast()).complete_json("area", AREA_SYSTEM, prompt, max_tokens=300)
        except Exception:
            return {}
        paths = raw.get("paths") if isinstance(raw, dict) else []
        why = str(raw.get("why", "")) if isinstance(raw, dict) else ""
        confidence = str(raw.get("confidence", "low")).lower() if isinstance(raw, dict) else "low"
        cached = _json.dumps({"paths": [str(p) for p in (paths or []) if isinstance(p, str)][:3], "why": why[:200],
                              "confidence": confidence})
        store.cache_set(repo, "model_area", key, cached)
    try:
        data = _json.loads(cached)
    except ValueError:
        return {}
    if data.get("confidence") != "high":
        return {}
    out: dict[str, PathHit] = {}
    for i, p in enumerate(data.get("paths", [])[:3]):
        p = p.strip().lstrip("/")
        weight = MODEL_AREA_WEIGHT - 0.1 * i
        if p in tree.files:
            _add(out, p, weight, f"the model reads the question as being about {p}: {data.get('why', '')}".rstrip(": "))
        elif p.rstrip("/") + "/" in tree.dirs:
            _add(out, p.rstrip("/") + "/", weight,
                 f"the model reads the question as being about {p.rstrip('/')}/: {data.get('why', '')}".rstrip(": "))
    return out
