"""Idempotent development bootstrap for the Docker stack."""
import os
import shutil
import subprocess
from pathlib import Path

from .auth import Auth
from .database import connect
from .ingest import _git as _read_git, index_repo
from .store import Store


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                            text=True, timeout=120, check=True)
    return result.stdout.strip()


def _demo_checkout() -> Path:
    """Create a real, persistent checkout from the bundled fixture."""
    source = Path(__file__).resolve().parent.parent / "fixtures" / "billing"
    repo = Path("/data/demo-repo")
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"],
                       check=True, capture_output=True, text=True, timeout=30)
    shutil.copytree(source, repo, dirs_exist_ok=True)
    _git(repo, "add", "-A")
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"],
                      check=False, timeout=30).returncode == 1:
        _git(repo, "-c", "user.name=Raven Demo", "-c", "user.email=demo@bridge.local",
             "commit", "-q", "-m", "chore: seed billing fixture",
             "-m", "Bundle a small billing checkout so the local ownership map and repository "
             "records are available immediately after the Docker stack starts.")
    return repo


def _ingest_if_changed(store: Store, repo: Path, name: str) -> None:
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} is not a Git checkout")
    head = _read_git(repo, "rev-parse", "HEAD").strip()
    key = f"bootstrap_ingest:{name}"
    fingerprint = f"v2:{repo.resolve()}:{head}"
    if store.graph.get_setting(key) == fingerprint:
        return
    stats = index_repo(store.graph, repo, repo_name=name, rev=head)
    store.graph.set_setting(key, fingerprint)
    print(f"Indexed {name}: {stats['files']} files, {stats['commits']} commits")


def _ingest_configured(store: Store, repo: Path, name: str) -> bool:
    """Ingest the checkout Raven was set up with. One that cannot be
    read is no reason to keep Raven down: measured on a fresh install,
    a checkout whose files the container could not read put the app in a
    restart loop, with the reason only in the logs. Raven starts without
    it and readiness says what went wrong."""
    try:
        _ingest_if_changed(store, repo, name)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        detail = ((getattr(error, "stderr", "") or "") if isinstance(error, subprocess.CalledProcessError)
                  else "") or str(error)
        first = next((line.strip() for line in detail.splitlines() if line.strip()), type(error).__name__)
        message = f"{name} at {repo}: {first}"[:300]
        store.graph.set_setting("bootstrap_ingest_error", message)
        print(f"Raven: could not ingest {message}; starting without it", flush=True)
        return False
    store.graph.set_setting("bootstrap_ingest_error", "")
    return True


def initialize_workspace(store: Store, name: str) -> None:
    """Optional operator-owned headless setup; no personal login required.

    The Docker operator already owns the database and deployment secrets.
    Keep its admin credential and limited agent credential distinct.
    """
    name = name.strip()[:100]
    if not name or store.graph.get_setting("workspace_name"):
        return
    if not (os.environ.get("BRIDGE_ADMIN_TOKEN") or any(p["role"] == "admin" for p in store.graph.people())):
        raise ValueError("Headless setup needs BRIDGE_ADMIN_TOKEN or the local Docker admin credential")
    with store.graph.transaction():
        store.graph.set_setting("workspace_name", name)
        store.graph.set_setting("workspace_profile_pending", "")
        store.graph.append_event("workspace_created", {"name": name, "via": "operator configuration"})


def main():
    target = os.environ["DATABASE_URL"]
    # Different from the write lock: seeding uses several connections.
    lock = connect(target, autocommit=True)
    try:
        lock.execute("SELECT pg_advisory_lock(724193803)")
        store = Store(target)
        try:
            checkout = os.environ.get("BRIDGE_INGEST_REPO_DIR", "").strip()
            custom_name = os.environ.get("BRIDGE_INGEST_REPO", checkout).strip() or checkout
            if os.environ.get("BRIDGE_DEMO", "0") == "1":
                store.seed()
                if not checkout or custom_name.lower() != "acme/platform":
                    _ingest_if_changed(store, _demo_checkout(), "acme/platform")
            if checkout:
                repo = Path("/repos") / checkout
                if repo.resolve().parent != Path("/repos"):
                    raise ValueError("BRIDGE_INGEST_REPO_DIR must name one directory under /repos")
                _ingest_configured(store, repo, custom_name)
            if os.environ.get("BRIDGE_DEV_LOGIN", "1") == "1" and not store.graph.get_setting('workspace_name', ''):
                token_path = Path(os.environ.get("BRIDGE_LOGIN_FILE", "/data/dev-login-token"))
                auth = Auth(store, enabled=True)
                token = token_path.read_text().strip() if token_path.exists() else ""
                if not token or not auth._token_person(token)[0]:
                    person_id = store.graph.add_person("Local developer", email="developer@bridge.local", role="admin")
                    credential = auth.create_token(person_id, "Docker development login", kind="human")
                    token_path.parent.mkdir(parents=True, exist_ok=True)
                    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as output:
                        output.write(credential["token"] + "\n")
                    token = credential["token"]
                person_id = auth._token_person(token)[0]
                agent_path = token_path.with_name("dev-agent-token")
                agent_token = agent_path.read_text().strip() if agent_path.exists() else ""
                agent_identity = auth._token_person(agent_token) if agent_token else ("", "", "")
                if agent_identity[0] != person_id or agent_identity[2] != "agent":
                    credential = auth.create_token(person_id, "Docker local MCP agent", kind="agent")
                    fd = os.open(agent_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as output:
                        output.write(credential["token"] + "\n")
            initialize_workspace(store, os.environ.get("BRIDGE_WORKSPACE_NAME", ""))
        finally:
            store.graph.close()
    finally:
        lock.close()


if __name__ == "__main__":
    main()


def dev_login(store):
    if store.graph.db.execute("SELECT 1 FROM account_passwords LIMIT 1").fetchone():
        return "Workspace already claimed. Sign in with your account."
    if os.environ.get("BRIDGE_DEV_LOGIN", "1") != "1":
        return "Development login is disabled. Sign in with your account."
    path = Path(os.environ.get("BRIDGE_LOGIN_FILE", "/data/dev-login-token"))
    token = path.read_text().strip() if path.exists() else ''
    if not token or not Auth(store, enabled=True)._token_person(token)[0]:
        return "No valid development credential. Run ./dev up to initialize the workspace."
    return token + "\nSign in at " + os.environ.get("BRIDGE_PUBLIC_URL", "http://localhost:7333").rstrip('/') + "/auth/login"
