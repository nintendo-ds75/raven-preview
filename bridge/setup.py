"""Configuration wizard run inside Docker; no host Python installation required."""
import argparse
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit

DEFAULT_PORT = "7333"
DEFAULT_PUBLIC_URL = f"http://localhost:{DEFAULT_PORT}"
LOOPBACK = ("localhost", "127.0.0.1", "::1")


def read_settings(path: Path) -> dict[str, str]:
    """The settings already saved, unquoted the way save_settings wrote them."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        value = match[2]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1].replace("\\'", "'")
        out[match[1]] = value
    return out


def align_public_url(settled: dict[str, str]) -> str:
    """The address to publish, given the port that is actually published.

    Docker publishes BRIDGE_PORT and the container always binds 7333, so
    the server learns the port people reach it on from BRIDGE_PUBLIC_URL
    and from nowhere else. When the two disagree, every request arrives
    with a Host the server was never told about and is refused; the
    printed link looks right and returns 403. A name that is not
    loopback means something is in front of this port and terminating on
    its own, so that one is left exactly as the operator set it."""
    port = settled.get("BRIDGE_PORT", DEFAULT_PORT)
    public = settled.get("BRIDGE_PUBLIC_URL", DEFAULT_PUBLIC_URL)
    parts = urlsplit(public)
    if (parts.hostname or "") not in LOOPBACK:
        return public
    return f"{parts.scheme or 'http'}://{parts.hostname}:{port}"


def save_settings(path: Path, updates: dict[str, str]) -> None:
    """Preserve unrelated settings/comments and atomically save private configuration."""
    original = path.read_text() if path.exists() else "# Raven local configuration (do not commit)\n"
    remaining = dict(updates)
    lines = []
    for line in original.splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in updates:
            if match[1] in remaining:
                lines.append(encode_setting(match[1], remaining.pop(match[1])))
        else:
            lines.append(line)
    lines.extend(encode_setting(key, value) for key, value in remaining.items())
    fd, temporary = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def encode_setting(key: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{key} must be a single line")
    # Compose single-quoted values are literal, including dollar signs.
    return key + "='" + value.replace("'", "\\'") + "'"


def _save_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bridge-mcp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(value)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mcp_clients(project: Path, url: str, token: str, clients: list[str]) -> list[str]:
    """Install URL-based MCP entries without exposing credentials to Git."""
    if not token or not url.startswith(("http://localhost:", "http://127.0.0.1:", "https://")):
        raise ValueError("MCP needs an agent token and a local or HTTPS Raven URL")
    config = {"type": "http", "url": url, "headers": {"Authorization": "Bearer " + token}}
    paths = {client: project / (".mcp.json" if client == "claude" else
             ".cursor/mcp.json" if client == "cursor" else ".codex/config.toml") for client in clients}
    for path in paths.values():
        relative = path.relative_to(project).as_posix()
        tracked = subprocess.run(["git", "-C", str(project), "ls-files", "--error-unmatch", "--", relative],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        if tracked:
            raise ValueError(f"Refusing to put an agent token in tracked file {path}; use a private client config")
    written = []
    for client in clients:
        path = paths[client]
        relative = path.relative_to(project).as_posix()
        exclude = project / ".git/info/exclude"
        if exclude.exists():
            existing = exclude.read_text()
            entry = "/" + relative
            if entry not in existing.splitlines():
                with exclude.open("a") as output:
                    output.write(("\n" if existing and not existing.endswith("\n") else "") + entry + "\n")
        if client in ("claude", "cursor"):
            current = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(current, dict) or not isinstance(current.get("mcpServers", {}), dict):
                raise ValueError(f"Cannot update invalid MCP configuration at {path}")
            current.setdefault("mcpServers", {})["bridge"] = (config if client == "claude" else
                {"url": url, "headers": config["headers"]})
            _save_text(path, json.dumps(current, indent=2) + "\n")
        elif client == "codex":
            original = path.read_text() if path.exists() else ""
            if original:
                tomllib.loads(original)
            headings = list(re.finditer(r"(?m)^\[([^]]+)\]\s*$", original))
            chunks = []
            start = 0
            for index, heading in enumerate(headings):
                end = headings[index + 1].start() if index + 1 < len(headings) else len(original)
                if heading.group(1) == "mcp_servers.bridge" or heading.group(1).startswith("mcp_servers.bridge."):
                    chunks.append(original[start:heading.start()])
                    start = end
            chunks.append(original[start:])
            kept = "".join(chunks).rstrip()
            entry = (f'[mcp_servers.bridge]\nurl = {json.dumps(url)}\n'
                     f'http_headers = {{ Authorization = {json.dumps("Bearer " + token)} }}\n')
            _save_text(path, (kept + "\n\n" if kept else "") + entry)
        written.append(str(path))
    return written


def connect_mcp(settings, project, token, host_path=""):
    clients = [n for n in settings.get("BRIDGE_MCP_CLIENTS", "").split(",") if n]
    url = align_public_url(settings).rstrip('/') + '/mcp'
    written = _mcp_clients(project, url, token, clients)
    if host_path:
        save_settings(Path('.env'), {'BRIDGE_AGENT_PROJECT': host_path})
    return [str(Path(host_path) / Path(p).relative_to(project)) if host_path else p for p in written]


def configure(args, path: Path, interactive: bool) -> None:
    updates = {}
    if getattr(args, "workspace", None):
        updates["BRIDGE_WORKSPACE_NAME"] = args.workspace.strip()[:100]
    guided = interactive
    if guided:
        print("Raven starts empty. Use --demo if you want sample tasks and owners.")
        print("Press Enter to skip a source or keep its saved settings. Secrets are hidden.")
        if args.repo and not args.repo_name:
            args.repo_name = input("Repository identity (owner/name; Enter for directory name): ").strip() or None
    if args.port is not None:
        if not 1 <= args.port <= 65535:
            raise ValueError("--port must be between 1 and 65535")
        updates["BRIDGE_PORT"] = str(args.port)
    updates["BRIDGE_DEMO"] = "1" if args.demo else "0"
    if args.repo:
        repo = PurePosixPath(args.repo)
        if not repo.is_absolute() or not repo.name:
            raise ValueError("Repository must be an absolute macOS/Linux/WSL path")
        updates.update(BRIDGE_REPOS_DIR=str(repo.parent), BRIDGE_INGEST_REPO_DIR=repo.name,
                       BRIDGE_INGEST_REPO=args.repo_name or repo.name)
    elif args.repo_name:
        raise ValueError("--repo-name requires --repo")
    clients = []
    if guided:
        raw_clients = input("Connect agent MCP clients (claude,cursor,codex; Enter to skip): ").lower()
        clients = [value.strip() for value in raw_clients.split(",") if value.strip()]
        unknown = set(clients) - {"claude", "cursor", "codex"}
        if unknown:
            raise ValueError("Unknown agent client: " + ", ".join(sorted(unknown)))
        clients = list(dict.fromkeys(clients))

        def ask(label, fields):
            if input(f"Configure {label}? [y/N] ").strip().lower() not in ("y", "yes"):
                return False
            for key, prompt, secret in fields:
                value = (getpass.getpass(prompt + ": ") if secret else input(prompt + ": ")).strip()
                if value:
                    updates[key] = value
            return True

        print("To connect GitHub, open Connections & setup after startup and choose repositories on GitHub.")
        if ask("Anthropic model-assisted retrieval", [("ANTHROPIC_API_KEY", "Anthropic API key", True),
            ("ANTHROPIC_WORKSPACE_ID", "Anthropic workspace ID (optional, for multi-workspace keys; Enter to keep/skip)", False)]):
            if updates.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"):
                updates["BRIDGE_MODEL_API"] = "anthropic"
            else:
                print("No Anthropic key supplied; keeping the existing retrieval mode.")
        print("Slack is the normal human interface: contacts are discovered automatically; personal accounts are optional.")
        ask("Slack delivery", [("SLACK_BOT_TOKEN", "Slack bot token", True),
            ("SLACK_SIGNING_SECRET", "Slack signing secret", True),
            ("SLACK_FALLBACK_CHANNEL", "Triage channel ID (invite the bot)", False)])
        ask("Teams delivery (outbound only, used when Slack is not configured; people answer in the inbox)", [
            ("TEAMS_WEBHOOK_URL", "Teams webhook URL", True)])
        if ask("managed coding-agent execution", [("OPENAI_API_KEY", "OpenAI API key", True)]):
            if updates.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"):
                updates.update(BRIDGE_INSTALL_AGENTS="true", BRIDGE_AGENTS="1")
            else:
                print("No OpenAI key supplied; keeping the existing execution mode.")
    settled = {**read_settings(path), **updates}
    if clients and settled.get("BRIDGE_DEV_LOGIN", "1") != "1":
        raise ValueError("Automatic MCP setup needs BRIDGE_DEV_LOGIN=1; shared deployments mint agent tokens in the app")
    if clients:
        updates["BRIDGE_MCP_CLIENTS"] = ",".join(clients)
    aligned = align_public_url(settled)
    if aligned != settled.get("BRIDGE_PUBLIC_URL", DEFAULT_PUBLIC_URL):
        updates["BRIDGE_PUBLIC_URL"] = aligned
    if updates or not path.exists():
        save_settings(path, updates)
    if clients:
        print("Selected MCP clients will be connected to the Raven URL after startup.")
    print(f"Raven will be reachable at {aligned}; that is the address to open and the only host it accepts.")
    print("Configuration ready. Existing settings are retained in .env.")
    print("External connections become usable only with valid credentials and provider-side configuration.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--connect-mcp", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--repo")
    parser.add_argument("--repo-name")
    parser.add_argument("--workspace", help="Create the workspace without a browser or personal account")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    try:
        if args.connect_mcp:
            saved = read_settings(Path(".env"))
            clients = [name for name in saved.get("BRIDGE_MCP_CLIENTS", "").split(",") if name]
            if clients:
                token = sys.stdin.read().strip()
                url = align_public_url(saved).rstrip("/") + "/mcp"
                written = connect_mcp(saved, Path("/agent-project"), token, os.environ.get("BRIDGE_AGENT_PROJECT_HOST", ""))
                print("Connected your agent to Raven over MCP in: " + ", ".join(written))
            return
        configure(args, Path(".env"), not args.yes and sys.stdin.isatty() and sys.stdout.isatty())
    except (ValueError, EOFError, KeyboardInterrupt) as error:
        parser.exit(1, f"Setup stopped: {error}\n")


if __name__ == "__main__":
    main()
