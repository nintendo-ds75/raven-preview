"""Load the synthetic org into a fresh database.

Signed decisions become approved decisions, open tickets become pending
decisions (both backdated to their recorded dates), CODEOWNERS entries,
blame shares and review counts become ownership rows, and the merged PRs
become record intents. Writes hardtest.db and codes.json next to this file.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).parent))
from bridge.store import Store  # noqa: E402
import org  # noqa: E402

DB = Path(__file__).parent / "hardtest.db"
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DB) + suffix)
    if p.exists():
        p.unlink()
store = Store(DB)
graph = store.graph
REPO = "platform"

owner_ids = {}
for name, team, patterns in org.OWNERS:
    owner_ids[name] = store.add_owner({"name": name, "team": team, "patterns": patterns})["id"]
    graph.upsert_engineer(name)

codes = {}
backdate = []
run = store.add_run({"title": "Historical decisions import", "agent": "Import", "repo": "acme/platform"})
for code, date, owner, path, question, answer, rationale in org.DECISIONS:
    d = store.request({"run_id": run["id"], "question": question, "context": f"[{code}] Recorded {date}. Path {path}.",
                       "path": path, "owner_id": owner_ids[owner]})
    store.answer(d["id"], {"answer": answer, "rationale": rationale})
    codes[d["id"]] = code
    backdate.append((d["id"], date))
store.update_run(run["id"], {"status": "completed"})

trun = store.add_run({"title": "Open tickets import", "agent": "Import", "repo": "acme/platform"})
for code, date, filed_by, path, question, context in org.TICKETS:
    d = store.request({"run_id": trun["id"], "question": question, "context": f"[{code}] {context}", "path": path})
    codes[d["id"]] = code
    backdate.append((d["id"], date))

# Ownership graph: CODEOWNERS, blame shares, review counts (new capability).
for line in org.CODEOWNERS.splitlines():
    if not line.strip():
        continue
    pattern, handle = line.split()
    prefix = pattern.rstrip("*")
    graph.set_ownership(REPO, prefix, handle.lstrip("@"), "codeowners", 1.0, f"CODEOWNERS lists {handle} for {pattern}")
for path, shares in org.BLAME.items():
    prefix = path.rsplit("/", 1)[0] + "/"
    for eng, share in shares.items():
        graph.set_ownership(REPO, prefix, eng, "blame", share, f"{round(share * 100)}% of the commit touches under {prefix}")
for path, counts in org.REVIEWS.items():
    total = sum(counts.values())
    for eng, n in counts.items():
        graph.set_ownership(REPO, "", eng, "review", n / total, f"named as reviewer on {n} of the {total} reviewed changes")
# Merged PRs as records (new capability).
for ref, date, author, title, paths in org.MERGED_PRS:
    graph.upsert_intent(REPO, "pr", ref, title, f"Merged {date}. Files: {', '.join(paths)}.", author, f"{date}T12:00:00+00:00")

graph.close()
db = sqlite3.connect(DB)
for did, date in backdate:
    ts = f"{date}T12:00:00+00:00"
    db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (ts, ts, did))
    db.execute("UPDATE decision_revisions SET created_at=? WHERE decision_id=?", (ts, did))
db.commit()
db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
db.close()
(Path(__file__).parent / "codes.json").write_text(json.dumps(codes, indent=1))
print(f"Loaded {len(org.DECISIONS)} approved decisions, {len(org.TICKETS)} open tickets, {len(org.OWNERS)} owners, "
      f"{graph.counts()['ownership']} ownership rows, {graph.counts()['intents']} PR records into {DB}")
