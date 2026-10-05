import argparse
import json
import os
import sys
from pathlib import Path

from .mcp import serve_stdio
from .server import make_server
from .store import Invalid, Store


def main():
    parser = argparse.ArgumentParser(description="Bridge: human judgment for coding agents")
    parser.add_argument("command", nargs="?", choices=["serve", "mcp", "ingest", "ask", "sync", "backup"], default="serve")
    parser.add_argument("target", nargs="?", help="ingest: path to a git checkout; ask: the question; sync: owner/name on GitHub; "
                                                  "backup: the file to write")
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL") or str(Path(__file__).resolve().parent.parent / ".bridge" / "bridge.db"),
                        help="SQLite path or PostgreSQL URL (defaults to DATABASE_URL when set)")
    parser.add_argument("--port", type=int, default=7331)
    parser.add_argument("--host", default="127.0.0.1", help="Address to bind; anything but loopback needs auth on")
    parser.add_argument("--public-url", default="", help="The URL people reach this Bridge at (or BRIDGE_PUBLIC_URL)")
    parser.add_argument("--auth", choices=["on", "off"], default=None,
                        help="Require an identity on every request (or BRIDGE_AUTH); on by default off loopback")
    parser.add_argument("--demo", action="store_true", help="Seed illustrative tasks into an empty database")
    parser.add_argument("--repo", default="", help="ingest: repository name override; ask: repository scope")
    parser.add_argument("--path", default="unknown", help="ask: repository-relative file path for routing")
    parser.add_argument("--commits", type=int, default=None, help="ingest: history depth (0 = all)")
    parser.add_argument("--agents", action="store_true", help="Enable managed Agents API execution for the disposable billing fixture")
    parser.add_argument("--managed-repo", action="append", default=[], metavar="NAME=PATH",
                        help="A local git checkout the inbox may launch a managed task on (repeatable; needs --agents)")
    parser.add_argument("--agents-model", default="gpt-6-astra")
    parser.add_argument("--api-key-file", help="Optional local key file; otherwise read OPENAI_API_KEY")
    parser.add_argument("--webhook-port", type=int, help="Separate signed webhook ingress on loopback; requires OPENAI_WEBHOOK_SECRET")
    args = parser.parse_args()
    store = Store(args.db)
    if args.demo:
        store.seed()
    if args.command == "mcp":
        serve_stdio(store)
        return
    if args.command == "ingest":
        if not args.target:
            parser.error("ingest needs the path to a git checkout")
        from .ingest import index_repo
        stats = index_repo(store.graph, args.target, max_commits=args.commits, repo_name=args.repo)
        print(json.dumps(stats, indent=1))
        return
    if args.command == "backup":
        if not args.target:
            parser.error("backup needs the file to write")
        print(json.dumps(store.backup(args.target), indent=1))
        return
    if args.command == "sync":
        if not args.target:
            parser.error("sync needs the repository as owner/name")
        from .github import GitHubAPI, GitHubError, sync_repo
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not token:
            parser.error("sync needs GITHUB_TOKEN in the environment (a token that can read the repository, "
                         "its pull requests and the organization's teams)")
        try:
            print(json.dumps(sync_repo(store.graph, GitHubAPI(token), args.target), indent=1))
        except GitHubError as error:
            parser.error(str(error))
        return
    if args.command == "ask":
        if not args.target:
            parser.error("ask needs a question")
        from .config import load
        from .ladder import ask
        cfg = load()
        run = store.add_run({"title": args.target[:300], "agent": "bridge ask", "repo": args.repo or "local"})
        row = ask(store, cfg, run["id"], args.target, context="asked from the command line", path=args.path)
        print(json.dumps({k: row.get(k) for k in ("id", "status", "kind", "answer", "prediction", "evidence",
                                                   "owner_name", "owner_evidence", "note")}, indent=1))
        if not cfg.semantic_retrieval:
            print("(deterministic rungs only: no ANTHROPIC_API_KEY or claude CLI, or BRIDGE_SEMANTIC=0)")
        return
    executions, client, webhook = None, None, None
    if args.agents:
        from .agents_api import AgentsAPI, ConfigurationError, create_client
        from .execution import ExecutionService, parse_repositories
        try:
            repositories = parse_repositories(args.managed_repo)
            client = create_client(args.api_key_file)
            executions = ExecutionService(store, AgentsAPI(client), args.agents_model, repositories=repositories)
            executions.start()
            if args.webhook_port is not None:
                from .webhooks import start_webhooks
                webhook = start_webhooks(executions, client, args.webhook_port)
        except (ConfigurationError, ValueError, Invalid) as error:
            if executions:
                executions.close()
            if client:
                client.close()
            parser.error(str(error))
    from .auth import Auth
    from .server import LOOPBACK
    auth_flag = args.auth or os.environ.get("BRIDGE_AUTH", "").lower() or ("off" if args.host in LOOPBACK else "on")
    auth = Auth(store, enabled=auth_flag == "on", public_url=args.public_url)
    if auth.enabled and not auth.bootstrap_token and not auth.github_configured and not store.graph.people():
        parser.error("auth is on but nobody can sign in yet: set BRIDGE_ADMIN_TOKEN (a bootstrap admin token) or "
                     "BRIDGE_GITHUB_CLIENT_ID and BRIDGE_GITHUB_CLIENT_SECRET")
    delivery = None
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    teams_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if slack_token:
        from .delivery import SlackTransport
        delivery = store.connect_delivery(SlackTransport(slack_token),
                                          fallback_channel=os.environ.get("SLACK_FALLBACK_CHANNEL", "").strip(),
                                          base_url=auth.public_url or args.public_url)
        delivery.start()
    elif teams_url:
        store.graph.set_setting("slack_connected", "")
        from .delivery import TeamsTransport
        delivery = store.connect_delivery(TeamsTransport(teams_url), base_url=auth.public_url or args.public_url)
        delivery.start()
    else:
        store.graph.set_setting("slack_connected", "")
    from .github import GitHubAPI, Syncer
    from .github_device import connection_for
    app_file = Path(os.environ.get("BRIDGE_GITHUB_APP_FILE") or
                    ("/data/bridge-github-app.json" if Path("/data").is_dir() else ".bridge/github-app.json"))
    github_app = connection_for(store.graph, app_file)
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    token_api = GitHubAPI(github_token) if github_token else None
    syncer = Syncer(store.graph, lambda repo: github_app.api_for_repo(repo) or token_api,
                    repos=[r.strip() for r in os.environ.get("BRIDGE_GITHUB_REPOS", "").split(",") if r.strip()],
                    minutes=float(os.environ.get("BRIDGE_GITHUB_SYNC_MINUTES", "15") or 15))
    syncer.start()
    try:
        server = make_server(store, args.port, executions, host=args.host, auth=auth, public_url=args.public_url,
                             github_app=github_app, github_syncer=syncer)
    except Exception as error:
        parser.error(str(error))
    shown = auth.public_url or f"http://{args.host}:{server.server_port}"
    print(f"Bridge is running at {shown}" + (" (auth on)" if auth.enabled else "")
          + (f" ({delivery.channel.title()} delivery on)" if delivery else "")
          + (" (GitHub sync on)" if github_token or github_app.status()["repositories"] else ""),
          flush=True)
    from .database import display_location
    print(f"Database: {display_location(store.path)}", flush=True)
    import signal

    def stop(signum, frame):
        # SIGTERM is how a container or service manager stops Bridge.
        # Leave serve_forever the same way Ctrl-C does, so every open wait
        # is told the server stopped before the process goes.
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, stop)
    except ValueError:
        pass  # not the main thread
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        left = server.stop_waits()
        if left:
            print(f"Bridge: {left} wait{'s' if left != 1 else ''} still open at shutdown", file=sys.stderr,
                  flush=True)
        server.server_close()
        if syncer:
            syncer.close()
        if delivery:
            delivery.close()
        if webhook:
            webhook.shutdown()
            webhook.server_close()
        if executions:
            executions.close()
        if client:
            client.close()


if __name__ == "__main__":
    main()
