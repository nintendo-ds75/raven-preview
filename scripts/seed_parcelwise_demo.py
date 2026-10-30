"""Seed a demo workspace for Parcelwise, a six-person shipping-label startup.

Builds a small git repository with eight months of history by the team,
then loads people, teams and the authority map into a Raven database and
ingests the repository through Raven's ordinary path. Everything the intro
chat reads afterwards comes from that real ingest, not from fixtures.

    python scripts/seed_parcelwise_demo.py --db .bridge/parcelwise.db --repo /tmp/parcelwise
    BRIDGE_DB=.bridge/parcelwise.db python -m bridge serve --port 7333
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PEOPLE = [
    # name, email, login, team, role
    ("Priya Shah", "priya@parcelwise.example", "priyashah", "Founders", "admin"),
    ("Marcus Lee", "marcus@parcelwise.example", "marcuslee", "Founders", "admin"),
    ("Jordan Okafor", "jordan@parcelwise.example", "jokafor", "Engineering", "member"),
    ("Lena Park", "lena@parcelwise.example", "lenapark", "Engineering", "member"),
    ("Sam Rivera", "sam@parcelwise.example", "samrivera", "Customer success", "member"),
    ("Theo Grant", "theo@parcelwise.example", "theogrant", "Engineering", "member"),
]
TEAMS = [
    ("Founders", "parcelwise/founders", ["Priya Shah", "Marcus Lee"]),
    ("Engineering", "parcelwise/eng", ["Marcus Lee", "Jordan Okafor", "Lena Park", "Theo Grant"]),
    ("Customer success", "parcelwise/cs", ["Sam Rivera", "Priya Shah"]),
]
AUTHORITY = [
    ("Priya Shah", "category", "pricing", "decides", "Plans, prices and discounts"),
    ("Marcus Lee", "path", "infra/*", "decides", ""),
    ("Marcus Lee", "category", "ops", "decides", "Incidents and postmortems"),
    ("Jordan Okafor", "path", "app/labels/*", "decides", "Label purchasing and carrier APIs"),
    ("Lena Park", "path", "web/*", "decides", "Merchant dashboard"),
    ("Theo Grant", "path", "app/shopify/*", "decides", ""),
    ("Priya Shah", "category", "customer", "decides", "Which customers and requests the company builds for"),
    ("Priya Shah", "category", "ux", "decides", "Product shape and positioning"),
    ("Sam Rivera", "category", "customer", "knows", "Merchant relationships, including Bloom & Co"),
]
CODEOWNERS = """# Parcelwise code owners
/app/labels/    @jokafor
/app/orders/    @jokafor @lenapark
/web/           @lenapark
/app/shopify/   @theogrant
/infra/         @marcuslee
/app/billing/   @priyashah @marcuslee
"""
# Past judgment from outside git, imported as ordinary records the ladder
# can cite: kind, ref, date, author, title, body, paths.
RECORDS = [
    ("slack", "founders-2026-03-14", "2026-03-14T16:02:00+00:00", "Priya Shah",
     "We don't build for one customer",
     "We don't build for one customer. Three paying accounts asking, or it waits.", []),
    ("doc", "postmortem-2026-06-02", "2026-06-02T18:30:00+00:00", "Marcus Lee",
     "Postmortem: labels bought at a stale rate",
     "We bought $4,100 of labels at a stale rate. Rule from here on: never buy a label before the "
     "merchant sees the price.", ["app/labels/"]),
    ("note", "founders-sync-2026-08-20", "2026-08-20T17:00:00+00:00", "Priya Shah",
     "Founders' sync: who we win with",
     "We win on the fastest morning for 1 to 5 person shops. No enterprise features, no new top-level pages.", ["web/"]),
    ("note", "pr-212", "2026-09-09T15:12:00+00:00", "Jordan Okafor",
     "PR #212: every label purchase goes through purchase_label()",
     "Every label purchase goes through purchase_label() with an idempotency key. No other code path buys labels.",
     ["app/labels/"]),
]

# day offset from 2026-03-02, author, files, subject
HISTORY = [
    (0, "Marcus Lee", ["infra/deploy.sh", "README.md"], "Initial skeleton and deploy script"),
    (2, "Marcus Lee", ["infra/db.py"], "Postgres connection pool"),
    (5, "Priya Shah", ["app/billing/plans.py"], "Starter and Growth plans"),
    (9, "Marcus Lee", ["app/labels/carriers/usps.py"], "USPS rate quotes behind a client"),
    (16, "Jordan Okafor", ["app/labels/purchase.py"], "Buy a single label from a quote"),
    (19, "Jordan Okafor", ["app/labels/pdf.py"], "Render 4x6 label PDFs"),
    (23, "Jordan Okafor", ["app/labels/carriers/ups.py"], "UPS client with negotiated rates"),
    (30, "Lena Park", ["web/orders.tsx"], "Orders table with filters"),
    (34, "Lena Park", ["web/label_modal.tsx"], "Buy label modal"),
    (41, "Jordan Okafor", ["app/labels/purchase.py", "app/labels/carriers/usps.py"], "Retry carrier timeouts once, then fail loudly"),
    (47, "Priya Shah", ["app/billing/plans.py"], "Per-label fee for Starter"),
    (52, "Theo Grant", ["app/shopify/sync.py"], "Import open Shopify orders"),
    (58, "Theo Grant", ["app/shopify/webhooks.py"], "Verify Shopify webhook HMAC"),
    (63, "Jordan Okafor", ["app/labels/void.py"], "Void unused labels within 24h"),
    (70, "Lena Park", ["web/orders.tsx"], "Show carrier and service on each order row"),
    (76, "Marcus Lee", ["infra/queue.py"], "Background job queue on Postgres"),
    (83, "Jordan Okafor", ["app/labels/carriers/fedex.py"], "FedEx Ground behind feature flag"),
    (90, "Theo Grant", ["app/shopify/sync.py"], "Write tracking numbers back to Shopify"),
    (97, "Jordan Okafor", ["app/labels/purchase.py"], "Idempotency keys on label purchase"),
    (104, "Lena Park", ["web/settings.tsx"], "Default package presets"),
    (111, "Priya Shah", ["app/billing/discounts.py"], "Annual discount for Growth"),
    (118, "Jordan Okafor", ["app/labels/carriers/rates.py"], "Cache rate quotes for 10 minutes"),
    (126, "Theo Grant", ["app/shopify/webhooks.py"], "Dedupe orders/updated bursts"),
    (133, "Marcus Lee", ["infra/queue.py"], "Retry queue jobs with backoff"),
    (140, "Lena Park", ["web/orders.tsx", "web/label_modal.tsx"], "Keyboard shortcuts on the orders page"),
    (147, "Jordan Okafor", ["app/labels/purchase.py", "app/labels/pdf.py"], "Merge label PDFs for printing"),
    (155, "Theo Grant", ["app/shopify/sync.py"], "Handle partially fulfilled orders"),
    (162, "Jordan Okafor", ["app/labels/carriers/usps.py"], "USPS address validation before purchase"),
    (170, "Lena Park", ["web/orders.tsx"], "Select multiple orders"),
    (176, "Marcus Lee", ["infra/metrics.py"], "Track label purchase latency"),
    (183, "Jordan Okafor", ["app/labels/purchase.py"], "Surface carrier errors to the merchant"),
    (190, "Theo Grant", ["app/shopify/webhooks.py"], "Back off when Shopify rate limits"),
    (197, "Lena Park", ["web/orders.tsx"], "Empty states for new merchants"),
    (205, "Priya Shah", ["app/billing/plans.py"], "Rename Growth to Pro"),
    (211, "Jordan Okafor", ["app/labels/carriers/rates.py", "app/labels/carriers/ups.py"], "Prefer cheapest rate within delivery window"),
    (218, "Theo Grant", ["app/shopify/sync.py"], "Sync order tags for routing rules"),
    (224, "Jordan Okafor", ["app/orders/batch.py"], "Draft batch model for multi-order purchase"),
    (229, "Lena Park", ["web/orders.tsx"], "Batch selection bar (behind flag)"),
    (233, "Jordan Okafor", ["app/orders/batch.py", "app/labels/purchase.py"], "Spike: buy labels for a batch sequentially"),
    (236, "Theo Grant", ["app/shopify/webhooks.py"], "Log webhook payload sizes"),
]
BASE = "2026-03-02T10:00:00+00:00"


def git(repo, *args, env=None):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env={**os.environ, **(env or {})})


def build_repo(repo: Path):
    from datetime import datetime, timedelta
    if repo.exists():
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    emails = {p[0]: p[1] for p in PEOPLE}
    start = datetime.fromisoformat(BASE)
    for index, (day, author, files, subject) in enumerate(HISTORY):
        if index == 1:
            (repo / ".github").mkdir(exist_ok=True)
            (repo / ".github" / "CODEOWNERS").write_text(CODEOWNERS)
            files = [*files, ".github/CODEOWNERS"]
        for name in files:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as handle:
                handle.write(f"# {subject}\n")
        when = (start + timedelta(days=day, hours=index % 7)).isoformat()
        env = {"GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": emails[author], "GIT_AUTHOR_DATE": when,
               "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": emails[author], "GIT_COMMITTER_DATE": when}
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", subject, env=env)


# The same judgment as signed answers on past tasks, so the ladder can settle
# new questions from them: date, who, title, question, paths, category, answer, rationale.
DECISIONS = [
    ("2026-03-14T16:05:00+00:00", "Priya Shah", "Custom export for one merchant",
     "A single merchant asked for a custom feature. Do we build it now?", "", "customer",
     "We don't build for one customer. Three paying accounts asking, or it waits.",
     "Building for one account pulls the roadmap toward whoever is loudest. Slack #founders, Mar 14."),
    ("2026-06-02T18:40:00+00:00", "Marcus Lee", "Stale-rate incident follow-up",
     "Can a label be bought before the merchant has seen its current price?", "app/labels/", "ops",
     "Never buy a label before the merchant sees the price.",
     "We bought $4,100 of labels at a stale rate. Incident postmortem, Jun 2."),
    ("2026-08-20T17:10:00+00:00", "Priya Shah", "Positioning after the August sync",
     "Should new workflows get their own top-level pages or enterprise-style controls?", "web/", "ux",
     "We win on the fastest morning for 1 to 5 person shops. No enterprise features, no new top-level pages.",
     "Founders' sync, Aug 20."),
    ("2026-09-09T15:20:00+00:00", "Jordan Okafor", "Idempotent label purchase",
     "May any code path other than purchase_label() buy a label?", "app/labels/", "",
     "Every label purchase goes through purchase_label() with an idempotency key.",
     "Retries without the key double-bought labels. PR #212, Sep 9."),
]


def seed_decisions(store):
    from bridge.authz import Actor
    from bridge.mcp import call_tool
    people = {p["name"]: p for p in store.graph.people()}
    for when, who, title, question, paths, category, answer, rationale in DECISIONS:
        task = call_tool(store, "bridge_start_task", {"title": title, "goal": question, "repo": REPO,
                                                       "client_key": "seed-" + when, **({"paths": paths} if paths else {})})
        node = {"task_id": task["task_id"], "question": question, "client_ref": "seed-" + when}
        if paths:
            node["paths"] = paths
        if category:
            node["category"] = category
        added = call_tool(store, "bridge_add_node", node)
        decision = store.get_decision(added["node_id"])
        person = people[who]
        actor = Actor.person(person, kind="session")
        actor.override = person.get("role") == "admin"
        store.answer(decision["id"], {"answer": answer, "rationale": rationale,
                                      "expected_updated_at": decision["updated_at"]}, actor=actor)
        with store.connect() as db:
            db.execute("UPDATE decisions SET created_at=?, updated_at=? WHERE id=?", (when, when, decision["id"]))
            db.execute("UPDATE runs SET status='completed', updated_at=? WHERE id=?", (when, task["task_id"]))


def rename(company: str):
    """Swap the company name everywhere the seed writes it: emails, team
    handles, CODEOWNERS and the repository name."""
    global PEOPLE, TEAMS, AUTHORITY, CODEOWNERS, REPO, COMPANY
    COMPANY = company
    slug = "".join(ch for ch in company.lower() if ch.isalnum()) or "parcelwise"
    PEOPLE = [(n, e.replace("parcelwise", slug), lg, t, r) for n, e, lg, t, r in PEOPLE]
    TEAMS = [(n, h.replace("parcelwise", slug), m) for n, h, m in TEAMS]
    CODEOWNERS = CODEOWNERS.replace("Parcelwise", company)
    REPO = f"{slug}/app"


REPO = "parcelwise/app"
COMPANY = "Parcelwise"


def seed(db_path: str, repo: Path):
    from bridge.ingest import index_repo
    from bridge.store import Store
    if Path(db_path).exists():
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
    store = Store(db_path)
    store.graph.set_setting("workspace_name", COMPANY)
    for name, email, login, team, role in PEOPLE:
        store.add_person({"name": name, "email": email, "github_login": login, "team": team, "role": role})
    for name, handle, members in TEAMS:
        store.add_team({"name": name, "handle": handle, "members": members})
    for who, kind, scope, role, note in AUTHORITY:
        store.add_authority({"who": who, "person": who, "scope_kind": kind, "scope": scope, "role": role,
                             "repo": REPO, "note": note})
    stats = index_repo(store.graph, repo, repo_name=REPO, max_commits=0)
    for kind, ref, created, author, title, body, paths in RECORDS:
        store.add_record({"repo": REPO, "kind": kind, "ref": ref, "created_at": created, "author": author,
                          "title": title, "body": body, "paths": paths, "status": "decided", "resolved": True})
    seed_decisions(store)
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=".bridge/parcelwise.db")
    parser.add_argument("--repo", default="/tmp/parcelwise-app")
    parser.add_argument("--existing", action="store_true",
                        help="Ingest the git checkout at --repo as it is instead of building one")
    parser.add_argument("--repo-name", default="", help="Repository name to record (default <company>/app)")
    parser.add_argument("--company", default="Parcelwise", help="Company name to use in place of Parcelwise")
    args = parser.parse_args()
    if args.company != "Parcelwise":
        rename(args.company)
    global REPO
    if args.repo_name:
        REPO = args.repo_name
    repo = Path(args.repo)
    if not args.existing:
        build_repo(repo)
    stats = seed(args.db, repo)
    print(f"Seeded {args.db}: {len(PEOPLE)} people, {len(TEAMS)} teams, {stats}")


if __name__ == "__main__":
    main()
