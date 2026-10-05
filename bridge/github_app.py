"""Browser-installed GitHub App for repository sync without personal tokens."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .github import API, GitHubAPI, GitHubError, register


def manifest_state(secret: bytes, person_id: str) -> str:
    payload = json.dumps({"person": person_id, "expires": int(time.time()) + 3600,
                          "nonce": secrets.token_urlsafe(16)}, separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_manifest_state(secret: bytes, state: str, person_id: str) -> bool:
    try:
        encoded, signature = state.split(".", 1)
        if not hmac.compare_digest(hmac.new(secret, encoded.encode(), hashlib.sha256).hexdigest(), signature):
            return False
        data = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return data.get("person") == person_id and int(data.get("expires", 0)) >= time.time()
    except (ValueError, TypeError, KeyError):
        return False


def _github_request(method: str, path: str, token: str = "", data=None, opener=None):
    body = json.dumps(data if data is not None else {}).encode() if method == "POST" else None
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "bridge-github-app"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
    try:
        with (opener or urllib.request.urlopen)(req, timeout=30) as response:
            return json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as error:
        raise GitHubError(f"GitHub rejected the App connection ({error.code})", error.code) from error
    except (urllib.error.URLError, OSError) as error:
        raise GitHubError(f"GitHub App connection failed: {error}") from error


def _jwt(app_id: int, pem: str) -> str:
    """Short-lived RS256 app JWT; the private key never leaves the server."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError as error:
        raise GitHubError("GitHub App support needs the cryptography package") from error

    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")

    now = int(time.time())
    unsigned = encode({"alg": "RS256", "typ": "JWT"}) + b"." + encode(
        {"iat": now - 30, "exp": now + 540, "iss": str(app_id)})
    try:
        key = serialization.load_pem_private_key(pem.encode(), password=None)
    except (TypeError, ValueError) as error:
        raise GitHubError("The stored GitHub App private key is invalid") from error
    signature = key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
    return (unsigned + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")).decode()


class GitHubAppConnector:
    def __init__(self, graph, credentials_path: Path, opener=None):
        self.graph = graph
        self.path = credentials_path
        self.opener = opener
        self._lock = threading.RLock()
        self._tokens = {}

    def credentials(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def installations(self) -> dict:
        try:
            return json.loads(self.graph.get_setting("github_app_installations", "{}"))
        except ValueError:
            return {}

    def status(self) -> dict:
        credentials = self.credentials()
        installations = self.installations()
        return {"configured": bool(credentials), "slug": credentials.get("slug", ""),
                "installations": [{"id": key, "account": value.get("account", ""),
                                   "repositories": value.get("repositories", [])}
                                  for key, value in installations.items()],
                "repositories": sorted({repo for value in installations.values()
                                        for repo in value.get("repositories", [])})}

    @staticmethod
    def manifest(public_url: str) -> dict:
        base = public_url.rstrip("/")
        return {"url": base, "description": "Read-only repository evidence for Bridge",
                "redirect_url": base + "/auth/github-app/created",
                "setup_url": base + "/auth/github-app/installed",
                "setup_on_update": True,
                "public": True,
                "default_permissions": {"contents": "read", "pull_requests": "read", "members": "read"}}

    def convert_manifest(self, code: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", code or ""):
            raise GitHubError("GitHub did not return a valid App setup code")
        result = _github_request("POST", f"/app-manifests/{code}/conversions", opener=self.opener)
        app_id, slug, pem = result.get("id"), result.get("slug", ""), result.get("pem", "")
        if not isinstance(app_id, int) or not re.fullmatch(r"[a-z0-9-]+", slug) or "PRIVATE KEY" not in pem:
            raise GitHubError("GitHub returned incomplete App credentials")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".github-app-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump({"id": app_id, "slug": slug, "pem": pem}, output)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {"id": app_id, "slug": slug}

    def _app_token(self) -> str:
        credentials = self.credentials()
        if not credentials:
            raise GitHubError("Connect a GitHub App first")
        return _jwt(credentials["id"], credentials["pem"])

    def installation_api(self, installation_id: int) -> GitHubAPI:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached[1] > time.time() + 120:
                return GitHubAPI(cached[0], opener=self.opener)
            payload = _github_request("POST", f"/app/installations/{installation_id}/access_tokens",
                                      self._app_token(), opener=self.opener)
            token = payload.get("token", "")
            if not token:
                raise GitHubError("GitHub did not issue an installation token")
            self._tokens[installation_id] = (token, time.time() + 3000)
            return GitHubAPI(token, opener=self.opener)

    def connect_installation(self, installation_id: int) -> dict:
        if installation_id <= 0:
            raise GitHubError("Invalid GitHub installation")
        installation = _github_request("GET", f"/app/installations/{installation_id}",
                                       self._app_token(), opener=self.opener)
        if installation.get("app_id") != self.credentials().get("id") or installation.get("suspended_at"):
            raise GitHubError("This installation does not belong to the connected Bridge App")
        api = self.installation_api(installation_id)
        repositories = []
        page = 1
        while True:
            payload, _ = api.get("/installation/repositories", {"per_page": 100, "page": page})
            batch = payload.get("repositories", []) if isinstance(payload, dict) else []
            repositories.extend(repo["full_name"] for repo in batch if repo.get("full_name"))
            if len(batch) < 100:
                break
            page += 1
        with self.graph.transaction():
            installed = self.installations()
            installed[str(installation_id)] = {"account": installation.get("account", {}).get("login", ""),
                                                "repositories": sorted(set(repositories))}
            self.graph.set_setting("github_app_installations", json.dumps(installed, sort_keys=True))
        for repo in repositories:
            register(self.graph, repo)
        return {"account": installation.get("account", {}).get("login", ""),
                "repositories": sorted(set(repositories))}

    def api_for_repo(self, repo: str) -> GitHubAPI | None:
        for identifier, installation in self.installations().items():
            if repo in installation.get("repositories", []):
                return self.installation_api(int(identifier))
        return None
