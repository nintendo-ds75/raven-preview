"""Fetch pinned public PR evidence. Read-only; needs no GitHub credential."""
import argparse
import json
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def fetch(out):
    spec = json.loads((HERE / "cases.json").read_text())
    out.mkdir(parents=True, exist_ok=True)
    for number in spec["prior_prs"] + [c["pr"] for c in spec["cases"]]:
        path = out / f"{number}.json"
        if path.exists():
            continue
        obj = {}
        for suffix in ("", "/reviews", "/files"):
            url = f"https://api.github.com/repos/{spec['repository']}/pulls/{number}{suffix}"
            request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "bridge-real-oss-evaluation"})
            with urllib.request.urlopen(request, timeout=30) as response:
                obj[suffix or "pr"] = json.load(response)
        path.write_text(json.dumps(obj, indent=2))
        print(number, obj["pr"]["title"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    fetch(parser.parse_args().out)
