"""Every document is reachable from the index, and every HTTP route the
server dispatches is in the reference: the literal route strings, the
routes it dispatches on a split path with the action named on the dispatch
line, and the few whose action it forwards to a module unnamed."""
import re
import unittest
from fixtures import ROOT

DOCS = ROOT / "docs"
_RELATIVE_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
# Literal route strings in the server's GET and POST dispatch, with any
# trailing slash a prefix match carries (`/api/context/sources/`) dropped.
_ROUTE = re.compile(r"""['"](/api/[A-Za-z0-9_/\-]+)['"]""")
# A split-path dispatch names its collection either on the line itself
# (`parts[:2] == ["api", "tasks"]`) or on the block it sits in
# (`path.startswith('/api/workbench/')`), and its action by position
# (`parts[3] == "tree"`, `parts[3] in {"answer", "assign"}`).
_PREFIX = re.compile(r"""\w*parts\[:(\d)\] == \[([^\]]*)\]|path\.startswith\(["'](/api/[a-z\-]+)/["']\)""")
_ACTION = re.compile(r"""\w*parts\[(\d)\]\s*(?:==\s*["']([a-z\-]+)["']|in\s*[{(]([^})]*)[})])""")
_WORD = re.compile(r"""["']([a-z\-]+)["']""")
# Routes whose action the server forwards to a module without naming it
# (`act(store, parts[2], parts[3], ...)`), so no regex can harvest them.
# Each must still be in the reference; add one here only with that reason.
_FORWARDED_ROUTES = (
    "/api/objectives/:id/pause|resume|cancel|checkpoint|dispatch|achieve",
    "/api/proposals/:id/accept|dismiss",
    "/api/meetings/:id/consent|import|background-sync|sync|revoke|reconcile|join|stop|delete-remote",
    "/api/workbench/:id/pause|resume|cancel",
)
# Literal strings in server.py that are not routes a client calls: none at
# the moment. Add a route here only with the reason it stays undocumented.
_INTERNAL_ROUTES = frozenset()


def _split_routes(server):
    """Every `/api/<collection>/:id/<action>` the server dispatches on a
    split path with the action named on the dispatch line."""
    routes, prefix = set(), None
    for line in server.splitlines():
        found = _PREFIX.search(line)
        if found:
            prefix = _WORD.findall(found.group(2)) if found.group(1) else found.group(3).strip("/").split("/")
        for match in _ACTION.finditer(line):
            if prefix is None or int(match.group(1)) != len(prefix) + 1:
                continue  # an action below a nested id (`parts[5]`) is documented with its parent
            for action in ([match.group(2)] if match.group(2) else _WORD.findall(match.group(3))):
                routes.add("/" + "/".join(prefix) + "/:id/" + action)
    return routes


def _documented(route, reference):
    """The route appears in the reference on its own, with any id name
    (`:run_id`), or inside an alternation such as `/api/x/:id/pause|resume`."""
    collection, action = route.rsplit("/:id/", 1)
    pattern = re.escape(collection) + r"/:[a-z_]+/(?:[a-z\-]+\|)*" + re.escape(action) + r"(?![a-z\-])"
    return re.search(pattern, reference) is not None


def _require_checkout():
    if not DOCS.is_dir() or not (ROOT / "bridge" / "server.py").exists():
        raise unittest.SkipTest("requires the source checkout and documentation")


def _reachable(start):
    """Every Markdown file a reader can get to from `start` by following
    relative links, transitively."""
    seen, todo = set(), [start.resolve()]
    while todo:
        page = todo.pop()
        if page in seen or not page.exists():
            continue
        seen.add(page)
        for target in _RELATIVE_LINK.findall(page.read_text()):
            if "://" in target or target.startswith("mailto:"):
                continue
            linked = (page.parent / target).resolve()
            if linked.suffix == ".md" and linked not in seen:
                todo.append(linked)
    return seen


class DocsReachableTests(unittest.TestCase):
    def test_every_document_is_reachable_from_the_index(self):
        _require_checkout()
        reachable = _reachable(DOCS / "README.md")
        unreachable = sorted(doc.name for doc in DOCS.glob("*.md")
                             if doc.name != "README.md" and doc.resolve() not in reachable)
        self.assertEqual(unreachable, [], f"documents no link from docs/README.md leads to: {unreachable}")

    def test_the_index_links_every_document_directly(self):
        _require_checkout()
        index = (DOCS / "README.md").read_text()
        linked = {(DOCS / target).resolve() for target in _RELATIVE_LINK.findall(index)}
        missing = sorted(doc.name for doc in DOCS.glob("*.md")
                         if doc.name != "README.md" and doc.resolve() not in linked)
        self.assertEqual(missing, [], f"documents docs/README.md does not list: {missing}")


class RoutesDocumentedTests(unittest.TestCase):
    def test_every_dispatched_route_is_in_the_reference(self):
        _require_checkout()
        server = (ROOT / "bridge" / "server.py").read_text()
        reference = (DOCS / "reference.md").read_text()
        routes = sorted({match.rstrip("/") for match in _ROUTE.findall(server)} - _INTERNAL_ROUTES)
        self.assertGreater(len(routes), 30, "the dispatch table should yield dozens of routes")
        missing = [route for route in routes if route not in reference]
        self.assertEqual(missing, [], f"routes bridge/server.py dispatches that docs/reference.md does not name: {missing}")

    def test_the_split_path_routes_are_in_the_reference(self):
        _require_checkout()
        server = (ROOT / "bridge" / "server.py").read_text()
        reference = (DOCS / "reference.md").read_text()
        routes = sorted(_split_routes(server))
        self.assertGreater(len(routes), 15, "the split-path dispatch should yield more than a dozen routes")
        for expected in ("/api/decisions/:id/reframe", "/api/tasks/:id/interviews", "/api/workbench/:id/reconcile"):
            self.assertIn(expected, routes, "the harvester must see routes the hand list used to miss")
        missing = [route for route in routes if not _documented(route, reference)]
        self.assertEqual(missing, [], f"routes dispatched on a split path that docs/reference.md does not name: {missing}")

    def test_the_forwarded_routes_are_in_the_reference(self):
        _require_checkout()
        reference = (DOCS / "reference.md").read_text()
        missing = [route for route in _FORWARDED_ROUTES if route not in reference]
        self.assertEqual(missing, [], f"routes whose action a module names that docs/reference.md does not list: {missing}")


if __name__ == "__main__":
    unittest.main()
