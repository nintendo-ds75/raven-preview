"""What a first install actually hands you: the address it prints, and
whether readiness tells the truth about the map.

Both of these were found by a reviewer running `./setup --yes --port
17433` against this repository. The setup printed a `127.0.0.1` URL and
configured `localhost`, and the printed URL answered 403; and readiness
dropped its missing-authority blocker as soon as any authority row
existed, including one recorded for a different repository and one that
had already expired. Neither needs Docker to reproduce: the first is the
server's own host check against a published port, the second is
`Store.readiness()` against a repository with ownership in it.

    python3 -m evals.real_oss_remote.probe_deployment
    python3 -m evals.real_oss_remote.probe_deployment --out results/deployment.json

Exits non-zero if any probe reports the unsafe outcome.
"""
import argparse
import json
import os
import tempfile
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ.setdefault("BRIDGE_MODEL_API", "none")
os.environ.setdefault("BRIDGE_SEMANTIC", "0")
os.environ.setdefault("BRIDGE_LIVE", "0")

from bridge.auth import Auth  # noqa: E402
from bridge.server import make_server  # noqa: E402
from bridge.setup import align_public_url  # noqa: E402
from bridge.store import Store  # noqa: E402

TOKEN = "deployment-probe-admin-token"


def reach(port: int, host: str) -> int:
    request = Request(f"http://127.0.0.1:{port}/api/me",
                      headers={"Host": host, "Authorization": f"Bearer {TOKEN}"})
    try:
        with urlopen(request, timeout=10) as response:
            return response.status
    except HTTPError as error:
        return error.code


def published_address(work: Path) -> list[dict]:
    """Docker publishes BRIDGE_PORT and the container binds 7333 whatever
    is published, so BRIDGE_PUBLIC_URL is the only thing that tells the
    server which port people reach it on. Two things have to hold: setup
    must not leave the port and the address disagreeing, and a Bridge
    published on loopback must answer to both spellings of it."""
    out = []
    os.environ["BRIDGE_ADMIN_TOKEN"] = TOKEN
    store = Store(work / "deployment.db")
    auth = Auth(store, enabled=True, public_url="http://localhost:17433")
    server = make_server(store, port=0, auth=auth)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for host, want in (("localhost:17433", 200), ("127.0.0.1:17433", 200), ("[::1]:17433", 200),
                           ("localhost:17434", 403), ("bridge.acme.test:17433", 403)):
            got = reach(server.server_port, host)
            out.append({"probe": "published address", "case": host, "expected": want, "got": got,
                        "ok": got == want,
                        "note": "the printed link and the configured host are the same machine"
                                if want == 200 else "another port or name speaks for nobody"})
    finally:
        server.shutdown()
        server.server_close()
    for case, settings, want in (
            ("port set, address left behind", {"BRIDGE_PORT": "17433"}, "http://localhost:17433"),
            ("both set and agreeing", {"BRIDGE_PORT": "17433",
                                       "BRIDGE_PUBLIC_URL": "http://localhost:17433"}, "http://localhost:17433"),
            ("a name in front of the port", {"BRIDGE_PORT": "17433",
                                             "BRIDGE_PUBLIC_URL": "https://bridge.acme.test"},
             "https://bridge.acme.test")):
        got = align_public_url(settings)
        out.append({"probe": "setup keeps them together", "case": case, "expected": want, "got": got,
                    "ok": got == want})
    return out


def effective_authority(work: Path) -> list[dict]:
    """Readiness has to reflect authority that applies here and now. A
    row for another repository covers nothing of this one's, and an
    expired row covers nothing anywhere; clearing the blocker on either
    told an operator the map was set up while every question still fell
    through to the coordinator."""
    import shutil
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
    from fixtures import template_db  # noqa: E402

    out = []
    template, _stats = template_db("qemulike")
    shutil.copyfile(template, work / "readiness.db")
    store = Store(work / "readiness.db")
    graph = store.graph

    def blocked() -> bool:
        return any(r["key"].startswith("no_authority") for r in store.readiness())

    out.append({"probe": "effective authority", "case": "a fresh ingest, nobody recorded",
                "expected": True, "got": blocked(), "ok": blocked()})
    with graph.transaction():
        pid = graph.add_person("Wes Chen", email="wes@qemu.example", slack_id="UWES")
        graph.add_authority("path", "billing/*", "decides", person_id=pid, repo="acme/other")
    out.append({"probe": "effective authority", "case": "authority for another repository",
                "expected": True, "got": blocked(), "ok": blocked()})
    with graph.transaction():
        graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid, effective_to="2020-01-01")
    out.append({"probe": "effective authority", "case": "an authority that has expired",
                "expected": True, "got": blocked(), "ok": blocked()})
    with graph.transaction():
        graph.add_authority("path", "hw/riscv/*", "knows", person_id=pid)
    out.append({"probe": "effective authority", "case": "somebody who knows the area but does not decide",
                "expected": True, "got": blocked(), "ok": blocked()})
    with graph.transaction():
        graph.add_authority("path", "hw/riscv/*", "decides", person_id=pid)
    out.append({"probe": "effective authority", "case": "somebody who decides, in force, here",
                "expected": False, "got": blocked(), "ok": not blocked()})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write the results as JSON")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="bridge-deployment-") as temporary:
        work = Path(temporary)
        results = published_address(work) + effective_authority(work)
    width = max(len(r["case"]) for r in results)
    for r in results:
        print(f"  {'ok  ' if r['ok'] else 'FAIL'}  {r['probe']:26}  {r['case']:{width}}  "
              f"expected {r['expected']}, got {r['got']}")
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} probes held")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2) + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
