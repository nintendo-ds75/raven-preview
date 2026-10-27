"""Every document is reachable from the index, and every HTTP route the
server dispatches is in the reference."""
import re
import unittest
from fixtures import ROOT

DOCS = ROOT / "docs"
_RELATIVE_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+)(?:#[^)]*)?\)")
# Literal route strings in the server's GET and POST dispatch, with any
# trailing slash a prefix match carries (`/api/context/sources/`) dropped.
_ROUTE = re.compile(r"""['"](/api/[A-Za-z0-9_/\-]+)['"]""")
# Routes the server dispatches on a split path rather than a literal string,
# so the regex cannot see them. Each must still be in the reference.
_DYNAMIC_ROUTES = (
    "/api/objectives/:id/pause|resume|cancel|checkpoint|dispatch|achieve",
    "/api/proposals/:id/accept|dismiss",
    "/api/meetings/:id/consent|import|background-sync|sync|revoke|reconcile|join|stop|delete-remote",
    "/api/workbench/:id/verification",
    "/api/workbench/:id/patch",
    "/api/tasks/:id/inquiry",
)
# Literal strings in server.py that are not routes a client calls: none at
# the moment. Add a route here only with the reason it stays undocumented.
_INTERNAL_ROUTES = frozenset()


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
        reference = (DOCS / "reference.md").read_text()
        missing = [route for route in _DYNAMIC_ROUTES if route not in reference]
        self.assertEqual(missing, [], f"routes dispatched on a split path that docs/reference.md does not name: {missing}")


if __name__ == "__main__":
    unittest.main()
