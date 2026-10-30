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
    ("Priya Shah", "priya@parcelwise.dev", "priyashah", "Founders", "admin"),
    ("Marcus Lee", "marcus@parcelwise.dev", "marcuslee", "Founders", "admin"),
    ("Jordan Okafor", "jordan@parcelwise.dev", "jokafor", "Engineering", "member"),
    ("Lena Park", "lena@parcelwise.dev", "lenapark", "Engineering", "member"),
    ("Sam Rivera", "sam@parcelwise.dev", "samrivera", "Customer success", "member"),
    ("Theo Grant", "theo@parcelwise.dev", "theogrant", "Engineering", "member"),
]
TEAMS = [
    ("Founders", "parcelwise/founders", ["Priya Shah", "Marcus Lee"]),
    ("Engineering", "parcelwise/eng", ["Marcus Lee", "Jordan Okafor", "Lena Park", "Theo Grant"]),
    ("Customer success", "parcelwise/cs", ["Sam Rivera", "Priya Shah"]),
]
AUTHORITY = [
    ("Priya Shah", "category", "pricing", "decides", "Plans, prices and discounts"),
    ("Marcus Lee", "path", "infra/*", "decides", ""),
    ("Jordan Okafor", "path", "labels/*", "decides", "Label purchasing and carrier APIs"),
    ("Jordan Okafor", "path", "carriers/*", "decides", ""),
    ("Lena Park", "path", "web/*", "decides", "Merchant dashboard"),
    ("Theo Grant", "path", "integrations/shopify/*", "decides", ""),
    ("Sam Rivera", "category", "customer", "knows", "Merchant relationships, including Bloom & Co"),
]
CODEOWNERS = """# Parcelwise code owners
/labels/        @jokafor
/carriers/      @jokafor @marcuslee
/orders/        @jokafor @lenapark
/web/           @lenapark
/integrations/  @theogrant
/infra/         @marcuslee
/billing/       @priyashah @marcuslee
"""
# day offset from 2026-03-02, author, files, subject
HISTORY = [
    (0, "Marcus Lee", ["infra/deploy.sh", "README.md"], "Initial skeleton and deploy script"),
    (2, "Marcus Lee", ["infra/db.py"], "Postgres connection pool"),
    (5, "Priya Shah", ["billing/plans.py"], "Starter and Growth plans"),
    (9, "Marcus Lee", ["carriers/usps.py"], "USPS rate quotes behind a client"),
    (16, "Jordan Okafor", ["labels/purchase.py"], "Buy a single label from a quote"),
    (19, "Jordan Okafor", ["labels/pdf.py"], "Render 4x6 label PDFs"),
    (23, "Jordan Okafor", ["carriers/ups.py"], "UPS client with negotiated rates"),
    (30, "Lena Park", ["web/orders.tsx"], "Orders table with filters"),
    (34, "Lena Park", ["web/label_modal.tsx"], "Buy label modal"),
    (41, "Jordan Okafor", ["labels/purchase.py", "carriers/usps.py"], "Retry carrier timeouts once, then fail loudly"),
    (47, "Priya Shah", ["billing/plans.py"], "Per-label fee for Starter"),
    (52, "Theo Grant", ["integrations/shopify/sync.py"], "Import open Shopify orders"),
    (58, "Theo Grant", ["integrations/shopify/webhooks.py"], "Verify Shopify webhook HMAC"),
    (63, "Jordan Okafor", ["labels/void.py"], "Void unused labels within 24h"),
    (70, "Lena Park", ["web/orders.tsx"], "Show carrier and service on each order row"),
    (76, "Marcus Lee", ["infra/queue.py"], "Background job queue on Postgres"),
    (83, "Jordan Okafor", ["carriers/fedex.py"], "FedEx Ground behind feature flag"),
    (90, "Theo Grant", ["integrations/shopify/sync.py"], "Write tracking numbers back to Shopify"),
    (97, "Jordan Okafor", ["labels/purchase.py"], "Idempotency keys on label purchase"),
    (104, "Lena Park", ["web/settings.tsx"], "Default package presets"),
    (111, "Priya Shah", ["billing/discounts.py"], "Annual discount for Growth"),
    (118, "Jordan Okafor", ["carriers/rates.py"], "Cache rate quotes for 10 minutes"),
    (126, "Theo Grant", ["integrations/shopify/webhooks.py"], "Dedupe orders/updated bursts"),
    (133, "Marcus Lee", ["infra/queue.py"], "Retry queue jobs with backoff"),
    (140, "Lena Park", ["web/orders.tsx", "web/label_modal.tsx"], "Keyboard shortcuts on the orders page"),
    (147, "Jordan Okafor", ["labels/purchase.py", "labels/pdf.py"], "Merge label PDFs for printing"),
    (155, "Theo Grant", ["integrations/shopify/sync.py"], "Handle partially fulfilled orders"),
    (162, "Jordan Okafor", ["carriers/usps.py"], "USPS address validation before purchase"),
    (170, "Lena Park", ["web/orders.tsx"], "Select multiple orders"),
    (176, "Marcus Lee", ["infra/metrics.py"], "Track label purchase latency"),
    (183, "Jordan Okafor", ["labels/purchase.py"], "Surface carrier errors to the merchant"),
    (190, "Theo Grant", ["integrations/shopify/webhooks.py"], "Back off when Shopify rate limits"),
    (197, "Lena Park", ["web/orders.tsx"], "Empty states for new merchants"),
    (205, "Priya Shah", ["billing/plans.py"], "Rename Growth to Pro"),
    (211, "Jordan Okafor", ["carriers/rates.py", "carriers/ups.py"], "Prefer cheapest rate within delivery window"),
    (218, "Theo Grant", ["integrations/shopify/sync.py"], "Sync order tags for routing rules"),
    (224, "Jordan Okafor", ["orders/batch.py"], "Draft batch model for multi-order purchase"),
    (229, "Lena Park", ["web/orders.tsx"], "Batch selection bar (behind flag)"),
    (233, "Jordan Okafor", ["orders/batch.py", "labels/purchase.py"], "Spike: buy labels for a batch sequentially"),
    (236, "Theo Grant", ["integrations/shopify/webhooks.py"], "Log webhook payload sizes"),
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


def rename(company: str):
    """Swap the company name everywhere the seed writes it: emails, team
    handles, CODEOWNERS and the repository name."""
    global PEOPLE, TEAMS, AUTHORITY, CODEOWNERS, REPO
    slug = "".join(ch for ch in company.lower() if ch.isalnum()) or "parcelwise"
    PEOPLE = [(n, e.replace("parcelwise", slug), lg, t, r) for n, e, lg, t, r in PEOPLE]
    TEAMS = [(n, h.replace("parcelwise", slug), m) for n, h, m in TEAMS]
    CODEOWNERS = CODEOWNERS.replace("Parcelwise", company)
    REPO = f"{slug}/app"


REPO = "parcelwise/app"


def seed(db_path: str, repo: Path):
    from bridge.ingest import index_repo
    from bridge.store import Store
    if Path(db_path).exists():
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)
    store = Store(db_path)
    for name, email, login, team, role in PEOPLE:
        store.add_person({"name": name, "email": email, "github_login": login, "team": team, "role": role})
    for name, handle, members in TEAMS:
        store.add_team({"name": name, "handle": handle, "members": members})
    for who, kind, scope, role, note in AUTHORITY:
        store.add_authority({"who": who, "person": who, "scope_kind": kind, "scope": scope, "role": role,
                             "repo": REPO, "note": note})
    stats = index_repo(store.graph, repo, repo_name=REPO, max_commits=0)
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
