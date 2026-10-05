"""Local authorization for a registered GitHub App; only its public ID is shared."""
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import urllib.request
import urllib.error

from .github import GitHubAPI, GitHubError, register

DEFAULT_CLIENT_ID = "Iv23linIklMTb9JM3cxo"
DEFAULT_APP_SLUG = "bridge-repository-access"


def connection_for(graph, app_file, environ=None):
    """Use the bundled public app, preserving existing operator-owned installs."""
    from .github_app import GitHubAppConnector
    environ = os.environ if environ is None else environ
    client_id = environ.get("BRIDGE_GITHUB_APP_CLIENT_ID", "").strip()
    slug = environ.get("BRIDGE_GITHUB_APP_SLUG", "").strip()
    if bool(client_id) != bool(slug):
        raise ValueError("Configure both the registered GitHub App client ID and slug")
    existing_app = GitHubAppConnector(graph, app_file)
    if not client_id and existing_app.credentials():
        return existing_app
    return GitHubDeviceConnector(graph, app_file.with_name("github-user.json"),
                                 client_id or DEFAULT_CLIENT_ID, slug or DEFAULT_APP_SLUG)


class GitHubDeviceConnector:
    def __init__(self, graph, path: Path, client_id: str, slug: str, opener=None, clock=time.time):
        if not client_id or not re.fullmatch(r"[a-z0-9-]+", slug):
            raise ValueError("Configure both the registered GitHub App client ID and slug")
        self.graph, self.path = graph, path
        self.client_id, self.slug = client_id, slug
        self.opener, self.clock = opener or urllib.request.urlopen, clock
        self.lock = threading.RLock()
        self.pending = {}

    def credentials(self):
        return {}  # App private keys are never used by this connector.

    def _read(self):
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        return data if data.get("client_id") == self.client_id else {}

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".github-user-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({**data, "client_id": self.client_id}, output)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self):
        with self.lock:
            saved = self._read()
            return {"configured": True, "mode": "device", "slug": self.slug,
                    "authorized": bool(saved.get("access_token")),
                    "installation_url": f"https://github.com/apps/{self.slug}/installations/new",
                    "repositories": saved.get("repositories", []), "installations": []}

    def _oauth(self, path, values):
        request = urllib.request.Request("https://github.com" + path,
            data=json.dumps({"client_id": self.client_id, **values}).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "User-Agent": "bridge-github-device"}, method="POST")
        try:
            with self.opener(request, timeout=30) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise GitHubError("Could not reach GitHub authorization; please retry") from error

    def start_device(self, person):
        with self.lock:
            now = self.clock()
            self.pending = {key: value for key, value in self.pending.items() if value["expires"] > now}
            if len(self.pending) >= 100:
                raise GitHubError("Too many pending connections; please try again later")
            result = self._oauth("/login/device/code", {})
            if not result.get("device_code") or not result.get("user_code"):
                raise GitHubError("GitHub device authorization is unavailable. The app owner must enable Device Flow.")
            key = secrets.token_urlsafe(24)
            interval = max(5, int(result.get("interval", 5)))
            self.pending[key] = {"person": person, "code": result["device_code"],
                                 "expires": now + int(result.get("expires_in", 900)),
                                 "interval": interval, "next": now + interval}
            return {"flow_id": key, "user_code": result["user_code"],
                    "verification_uri": "https://github.com/login/device", "interval": interval}

    def poll_device(self, person, key):
        with self.lock:
            flow = self.pending.get(key)
            if not flow or flow["person"] != person:
                raise GitHubError("This connection was not started by your Raven account")
            now = self.clock()
            if now >= flow["expires"]:
                del self.pending[key]
                raise GitHubError("GitHub connection expired; start again")
            if now < flow["next"]:
                return {"pending": True, "interval": flow["interval"]}
            flow["next"] = now + flow["interval"]
            result = self._oauth("/login/oauth/access_token", {
                "device_code": flow["code"], "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
            error = result.get("error")
            if error in ("authorization_pending", "slow_down"):
                if error == "slow_down":
                    flow["interval"] += 5
                    flow["next"] = now + flow["interval"]
                return {"pending": True, "interval": flow["interval"]}
            del self.pending[key]
            if error or not result.get("access_token"):
                raise GitHubError("GitHub authorization was declined or expired; start again")
            self._save(self._token_data(result))
            return {"pending": False, **self.refresh_repositories()}

    def _token_data(self, result):
        return {"access_token": result["access_token"], "refresh_token": result.get("refresh_token", ""),
                "expires": self.clock() + int(result["expires_in"]) if result.get("expires_in") else 0,
                "repositories": []}

    def _api(self):
        data = self._read()
        if not data.get("access_token"):
            raise GitHubError("Authorize your GitHub account first")
        if data.get("expires") and self.clock() >= data["expires"] - 120:
            if not data.get("refresh_token"):
                raise GitHubError("GitHub authorization expired; reconnect your account")
            result = self._oauth("/login/oauth/access_token", {
                "grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            if result.get("error") or not result.get("access_token"):
                raise GitHubError("GitHub authorization expired or was revoked; reconnect your account")
            updated = self._token_data(result)
            updated["repositories"] = data.get("repositories", [])
            self._save(updated)
            data = updated
        return GitHubAPI(data["access_token"], opener=self.opener)

    def refresh_repositories(self):
        with self.lock:
            api = self._api()
            repos = []
            page = 1
            while True:
                payload, _ = api.get("/user/installations", {"per_page": 100, "page": page})
                installations = payload.get("installations", [])
                for installation in installations:
                    if installation.get("app_slug") != self.slug:
                        continue
                    repo_page = 1
                    while True:
                        result, _ = api.get(f"/user/installations/{int(installation['id'])}/repositories",
                                            {"per_page": 100, "page": repo_page})
                        batch = result.get("repositories", [])
                        repos.extend(item["full_name"] for item in batch)
                        if len(batch) < 100:
                            break
                        repo_page += 1
                if len(installations) < 100:
                    break
                page += 1
            saved = self._read()
            saved["repositories"] = sorted(set(repos))
            self._save(saved)
            for repo in saved["repositories"]:
                register(self.graph, repo)
            return {"repositories": saved["repositories"]}

    def api_for_repo(self, repo):
        with self.lock:
            if repo not in self._read().get("repositories", []):
                return None
            return self._api()
