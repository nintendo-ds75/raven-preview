"""Opt-in, single-tenant Teams channel bot; incoming webhooks remain outbound-only.

Implements Microsoft's Bot Connector REST authentication contract, with PyJWT's
RS256 verifier. No Emulator, unsigned callback, auto-link-by-name, Graph access,
or personal/group-chat support. See docs/teams.md for the deliberately narrow
installation contract and the live verification still required.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .delivery import Delivery, now_iso, retry_delay
from .store import Invalid

ISSUER = "https://api.botframework.com"
METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
KEYS_URL = "https://login.botframework.com/v1/.well-known/keys"
MAX_ATTEMPTS = 5


class TeamsAuthError(Invalid):
    """An untrusted or out-of-scope callback. Never include the bearer token."""


class TeamsUnavailable(RuntimeError):
    """Transient connector failure; no response body or secret is retained."""
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TeamsUnavailable("Teams endpoint redirected; request refused")


def _http(request):
    """TLS validation stays enabled and credentials never follow redirects."""
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            body = response.read(1_048_577)
        if len(body) > 1_048_576:
            raise TeamsUnavailable("Teams response exceeded limit")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except urllib.error.HTTPError as exc:
        delay = None
        if exc.code == 429:
            try:
                delay = float(exc.headers.get("Retry-After", "60"))
                if not math.isfinite(delay) or delay < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                delay = 60
        raise TeamsUnavailable(f"Teams endpoint HTTP {exc.code}", status=exc.code, retry_after=delay) from None
    except (urllib.error.URLError, ValueError, TimeoutError):
        raise TeamsUnavailable("Teams endpoint unavailable or malformed response") from None


def _guid(value, label):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise Invalid(f"{label} must be a UUID") from None


def _service_url(value):
    # This adapter supports the commercial cloud only. Pin the whole URL as
    # well as the host; do not turn a signed activity into arbitrary egress.
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "smba.trafficmanager.net"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not parsed.path.endswith("/")
            or not re.fullmatch(r"/[A-Za-z0-9/_-]+/", parsed.path)):
        raise Invalid("TEAMS_SERVICE_URL must be an HTTPS smba.trafficmanager.net endpoint with trailing slash")
    return value


@dataclass(frozen=True)
class TeamsConfig:
    app_id: str
    tenant_id: str
    channel_id: str
    client_secret: str = field(repr=False)
    people: dict[str, str] = field(default_factory=dict)
    service_url: str = "https://smba.trafficmanager.net/teams/"

    def __post_init__(self):
        object.__setattr__(self, "app_id", _guid(self.app_id, "TEAMS_APP_ID"))
        object.__setattr__(self, "tenant_id", _guid(self.tenant_id, "TEAMS_TENANT_ID"))
        _service_url(self.service_url)
        if not self.channel_id or len(self.channel_id) > 1024 or not self.client_secret:
            raise Invalid("Teams bot requires TEAMS_CHANNEL_ID and TEAMS_CLIENT_SECRET")
        if not isinstance(self.people, dict) or not self.people:
            raise Invalid("TEAMS_PEOPLE_JSON must map Entra object UUIDs to existing Raven person IDs")
        mapped = {_guid(k, "Teams person object ID"): v for k, v in self.people.items()}
        if any(not isinstance(v, str) or not v.strip() for v in mapped.values()):
            raise Invalid("Teams identity mappings require nonempty Raven person IDs")
        if len(set(mapped.values())) != len(mapped):
            raise Invalid("Each Teams identity must map to a distinct Raven person")
        object.__setattr__(self, "people", mapped)

    @property
    def bot_id(self):
        return "28:" + self.app_id

    @property
    def destination(self):
        # No colons: the shared reply reader splits its opaque channel:thread
        # reference. Real Teams IDs never enter that representation.
        return "teams-" + hashlib.sha256((self.tenant_id + self.app_id + self.channel_id).encode()).hexdigest()[:32]

    @classmethod
    def from_env(cls):
        names = ("TEAMS_APP_ID", "TEAMS_TENANT_ID", "TEAMS_CHANNEL_ID", "TEAMS_CLIENT_SECRET", "TEAMS_PEOPLE_JSON")
        values = {key: os.environ.get(key, "").strip() for key in names}
        if not any(values.values()):
            return None
        if not all(values.values()):
            raise Invalid("Teams bot configuration is incomplete: set " + ", ".join(names))
        try:
            people = json.loads(values["TEAMS_PEOPLE_JSON"])
        except ValueError:
            raise Invalid("TEAMS_PEOPLE_JSON must be a JSON object") from None
        return cls(values["TEAMS_APP_ID"], values["TEAMS_TENANT_ID"], values["TEAMS_CHANNEL_ID"],
                   values["TEAMS_CLIENT_SECRET"], people,
                   os.environ.get("TEAMS_SERVICE_URL", "https://smba.trafficmanager.net/teams/").strip())


class ConnectorAuth:
    """Validate every callback using Microsoft's fixed OpenID metadata."""
    def __init__(self, config, http=None):
        try:
            import jwt
        except ImportError:
            raise Invalid("Teams bot needs optional dependencies: pip install -r requirements-teams.txt") from None
        self.jwt, self.config, self.http = jwt, config, http or _http
        self.keys, self.refreshed, self.lock = [], 0.0, threading.Lock()

    def _key(self, kid):
        with self.lock:
            found = next((k for k in self.keys if k.get("kid") == kid), None)
            age = time.monotonic() - self.refreshed
            if not self.keys or age > 3600 or (found is None and age > 60):
                metadata = self.http(urllib.request.Request(METADATA_URL))
                if (metadata.get("issuer") != ISSUER or metadata.get("jwks_uri") != KEYS_URL
                        or "RS256" not in metadata.get("id_token_signing_alg_values_supported", [])):
                    raise TeamsUnavailable("Unexpected Bot Connector metadata")
                document = self.http(urllib.request.Request(KEYS_URL))
                keys = document.get("keys")
                if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
                    raise TeamsUnavailable("Malformed Bot Connector key set")
                self.keys, self.refreshed = keys, time.monotonic()
                found = next((k for k in keys if k.get("kid") == kid), None)
        if (found is None or found.get("kty") != "RSA" or found.get("use", "sig") != "sig"
                or found.get("alg", "RS256") != "RS256"
                or not isinstance(found.get("endorsements"), list)
                or "msteams" not in found["endorsements"]):
            raise TeamsAuthError("Teams signing key is unknown or lacks the Teams endorsement")
        return found

    def verify(self, authorization, activity):
        if not isinstance(authorization, str) or len(authorization) > 32768:
            raise TeamsAuthError("Invalid Teams authorization")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise TeamsAuthError("Teams bearer authentication required")
        try:
            header = self.jwt.get_unverified_header(parts[1])
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or header.get("crit"):
                raise TeamsAuthError("Invalid Teams signing algorithm or key")
            key = self.jwt.PyJWK.from_dict(self._key(header["kid"]), algorithm="RS256").key
            claims = self.jwt.decode(parts[1], key, algorithms=["RS256"], audience=self.config.app_id,
                issuer=ISSUER, leeway=60, options={"require": ["iss", "aud", "exp", "nbf", "serviceurl"], "strict_aud": True})
            if any(type(claims[k]) not in (int, float) for k in ("exp", "nbf")):
                raise TeamsAuthError("Invalid Teams token validity period")
            if not isinstance(activity, dict) or claims["serviceurl"] != activity.get("serviceUrl"):
                raise TeamsAuthError("Teams token service URL mismatch")
            if activity.get("serviceUrl") != self.config.service_url:
                raise TeamsAuthError("Teams service URL is outside this installation")
        except (self.jwt.PyJWTError, ValueError, TypeError, OverflowError):
            raise TeamsAuthError("Teams bearer token is invalid or expired") from None
        return claims


def migrate(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS teams_threads (
        id TEXT PRIMARY KEY, destination TEXT NOT NULL, conversation_id TEXT NOT NULL,
        root_activity_id TEXT NOT NULL, UNIQUE(destination, conversation_id));
        CREATE TABLE IF NOT EXISTS teams_ingress (
        id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL, thread_ts TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS teams_replies (
        id TEXT PRIMARY KEY, channel TEXT NOT NULL, thread_ts TEXT NOT NULL, text TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
        CREATE INDEX IF NOT EXISTS teams_ingress_pending ON teams_ingress(state,next_attempt);''')


class TeamsBotTransport:
    name = "teams"
    supports_dm = False

    def __init__(self, graph, config, http=None):
        self.graph, self.config, self.http = graph, config, http or _http
        self.token, self.token_until, self.lock = "", 0.0, threading.Lock()
        migrate(graph.db)

    def _access_token(self):
        with self.lock:
            if self.token and time.monotonic() < self.token_until:
                return self.token
            data = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.config.app_id,
                "client_secret": self.config.client_secret, "scope": ISSUER + "/.default"}).encode()
            response = self.http(urllib.request.Request(
                f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token", data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST"))
            if not response.get("access_token") or str(response.get("token_type", "")).lower() != "bearer":
                raise TeamsUnavailable("Microsoft did not issue a bot access token")
            try:
                lifetime = float(response["expires_in"])
            except (ValueError, TypeError, KeyError):
                raise TeamsUnavailable("Microsoft issued a token without a valid expiry") from None
            if lifetime <= 0:
                raise TeamsUnavailable("Microsoft issued an expired token")
            self.token, self.token_until = response["access_token"], time.monotonic() + max(0, lifetime - 60)
            return self.token

    def post_message(self, channel, text, blocks=None, thread_ts=""):
        return self._post_message(channel, text, blocks, thread_ts)

    def post_reply(self, channel, text, blocks, thread_ts, event_id):
        # Notification external_ref uses our opaque thread handle; a read-back
        # needs Microsoft's actual activity ID for exact reply-to binding.
        return self._post_message(channel, text, blocks, thread_ts, reply_ref=True)

    def _post_message(self, channel, text, blocks=None, thread_ts="", reply_ref=False):
        if channel != self.config.destination:
            raise Invalid("Teams destination does not match the pinned channel")
        text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"[\2](\1)", text)
        text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", text)
        if not thread_ts:
            text += "\n\nIn Teams, @mention this bot in the thread when replying."
        activity = {"type": "message", "text": text, "textFormat": "markdown"}
        if thread_ts:
            row = self.graph.db.execute("SELECT * FROM teams_threads WHERE id=? AND destination=?",
                                         (thread_ts, channel)).fetchone()
            if row is None:
                raise Invalid("Unknown Teams conversation reference")
            quote = lambda value: urllib.parse.quote(value, safe="")
            path = f"v3/conversations/{quote(row['conversation_id'])}/activities/{quote(row['root_activity_id'])}"
            activity["replyToId"] = row["root_activity_id"]
            payload = activity
        else:
            path = "v3/conversations"
            payload = {"isGroup": True, "bot": {"id": self.config.bot_id},
                "channelData": {"channel": {"id": self.config.channel_id}, "tenant": {"id": self.config.tenant_id}},
                "activity": activity}
        try:
            response = self.http(urllib.request.Request(self.config.service_url + path,
                data=json.dumps(payload).encode(), headers={"Content-Type": "application/json",
                "Authorization": "Bearer " + self._access_token()}, method="POST"))
        except TeamsUnavailable as exc:
            if exc.status == 401:
                with self.lock:
                    self.token, self.token_until = "", 0
            raise
        if not isinstance(response.get("id"), str) or not response["id"]:
            raise TeamsUnavailable("Teams accepted no message identifier")
        if thread_ts:
            return response["id"] if reply_ref else thread_ts
        if not isinstance(response.get("activityId"), str) or not response["activityId"]:
            raise TeamsUnavailable("Teams created no addressable channel thread")
        handle = uuid.uuid4().hex
        with self.graph.transaction():
            self.graph.db.execute("INSERT INTO teams_threads VALUES(?,?,?,?)",
                (handle, channel, response["id"], response["activityId"]))
        return handle


class TeamsDelivery(Delivery):
    """Reuse Raven's authority/read-back/scope checks for a verified person."""
    def __init__(self, store, config, transport=None, auth=None, base_url=""):
        self.config = config
        super().__init__(store, transport or TeamsBotTransport(store.graph, config),
                         fallback_channel=config.destination, base_url=base_url)
        migrate(store.graph.db)
        self.auth = auth or ConnectorAuth(config)
        self.inbox = TeamsInbox(self)
        for person_id in config.people.values():
            person = store.graph.get_person(person_id)
            if person is None or not person.get("active", 1):
                raise Invalid("Teams mapping names an unknown or inactive Raven person")
        # Publish only the opaque outbox address for a credential-free stdio
        # process. Incoming trust, identities, service URLs and secrets stay
        # with this configured server and are not inferred from stored metadata.
        with store.graph.transaction():
            store.graph.set_setting("slack_connected", "")
            store.graph.set_setting("teams_delivery", json.dumps({
                "version": 1, "mode": "bot", "destination": config.destination}))

    def _destination(self, person_name, person_id=""):
        from .routing import contact_for
        graph = self.store.graph
        # Shared enqueue/retry preserve an assigned person's stable identity.
        # Explicit recipients without an ID keep the existing contact lookup.
        if person_id:
            person = graph.get_person(person_id)
        else:
            person = contact_for(graph, person_name) if person_name else None
        if person is not None and not person["active"]:
            person = None
        return self.config.destination, (person or {}).get("id", ""), "", "channel"

    def slack_person(self, person_id):
        # Compatibility hook in the shared reply reader; never resolve a name,
        # email, Slack ID, or payload-supplied Raven ID as a Teams identity.
        if person_id not in self.config.people.values():
            return None
        person = self.store.graph.get_person(person_id)
        return person if person and person.get("active", 1) else None

    def _apply_reply(self, channel, thread_ts, user_id, text, action_token="", occurrence=None, event_id=""):
        note = self.notification_for_thread(channel, thread_ts)
        if note is None:
            return ""
        if not self.store.get_decision(note["decision_id"]).get("owner_id"):
            return "This question has no assigned contact. Route it in the Raven inbox before answering here."
        return super()._apply_reply(channel, thread_ts, user_id, text, action_token=action_token,
                                    occurrence=occurrence, event_id=event_id)

    def handle(self, authorization, activity):
        self.auth.verify(authorization, activity)
        return self.inbox.enqueue(activity)

    def inbound_failed(self):
        return [dict(r) for r in self.store.graph.db.execute(
            "SELECT id,'teams' channel,state,attempts,error,created_at received_at,created_at updated_at "
            "FROM teams_ingress WHERE error!='' ORDER BY created_at DESC LIMIT 200")]

    def reply_failures(self):
        return [dict(r) for r in self.store.graph.db.execute(
            "SELECT id,channel,thread_ts,state,attempts,error FROM teams_replies WHERE error!='' LIMIT 200")]

    def retry_inbound(self, event_id):
        receipt = self.store.graph.db.execute("SELECT state FROM webhook_receipts WHERE id=?", (event_id,)).fetchone()
        if receipt and receipt["state"] != "applied":
            raise Invalid("Teams outcome is uncertain. Inspect the decision and audit record before recovery; do not replay this approval")
        with self.store.graph.transaction():
            result = self.store.graph.db.execute("UPDATE teams_ingress SET state='queued',attempts=0,next_attempt=0,error='' "
                "WHERE id=? AND state='failed'", (event_id,))
        if not result.rowcount:
            raise Invalid("No failed Teams inbound event with that id")
        self.inbox.start()
        return {"id": event_id, "state": "queued"}


class TeamsInbox:
    def __init__(self, delivery):
        self.delivery, self.graph = delivery, delivery.store.graph
        self.thread, self.lock = None, threading.Lock()

    def enqueue(self, activity):
        config = self.delivery.config
        conversation, data = activity.get("conversation") or {}, activity.get("channelData") or {}
        sender, recipient = activity.get("from") or {}, activity.get("recipient") or {}
        if not all(isinstance(x, dict) for x in (conversation, data, sender, recipient)):
            raise TeamsAuthError("Malformed Teams activity context")
        tenant, channel = data.get("tenant") or {}, data.get("channel") or {}
        if not isinstance(tenant, dict) or not isinstance(channel, dict):
            raise TeamsAuthError("Malformed Teams installation context")
        if (activity.get("channelId") != "msteams" or tenant.get("id") != config.tenant_id
                or conversation.get("tenantId", config.tenant_id) != config.tenant_id
                or channel.get("id") != config.channel_id or conversation.get("conversationType") != "channel"
                or recipient.get("id") != config.bot_id):
            raise TeamsAuthError("Teams activity is outside the pinned tenant, channel, or bot")
        if activity.get("type") != "message":
            return {"ok": True, "ignored": "unsupported_activity"}
        if sender.get("role") in ("bot", "skill") or sender.get("id") == config.bot_id:
            raise TeamsAuthError("A Teams bot cannot act as a Raven decision-maker")
        try:
            oid = _guid(sender.get("aadObjectId", ""), "Teams sender object ID")
        except Invalid:
            raise TeamsAuthError("Teams member object ID is missing or malformed") from None
        person_id = config.people.get(oid)
        if not person_id or not self.delivery.slack_person(person_id):
            raise TeamsAuthError("Teams member has no active, explicit Raven identity mapping")
        eid, conversation_id, text = activity.get("id"), conversation.get("id"), activity.get("text")
        if (not isinstance(eid, str) or not eid or len(eid) > 1024
                or not isinstance(conversation_id, str) or not conversation_id or len(conversation_id) > 2048
                or not isinstance(text, str) or not text.strip() or len(text) > 12000):
            raise TeamsAuthError("Malformed Teams message")
        try:
            timestamp = datetime.fromisoformat(activity["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or not -300 <= time.time() - timestamp.timestamp() <= 86400:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise TeamsAuthError("Teams message timestamp is missing or outside the retry window") from None
        row = self.graph.db.execute("SELECT id FROM teams_threads WHERE destination=? AND conversation_id=?",
            (config.destination, conversation_id)).fetchone()
        if row is None:
            return {"ok": True, "ignored": "unknown_thread"}
        # Strip only this bot's structured mention. Other people's mentions
        # remain text and cannot change the verified actor.
        entities = activity.get("entities") or []
        if not isinstance(entities, list):
            raise TeamsAuthError("Malformed Teams entities")
        for entity in entities:
            if (isinstance(entity, dict) and entity.get("type") == "mention"
                    and isinstance(entity.get("mentioned"), dict) and entity["mentioned"].get("id") == config.bot_id
                    and isinstance(entity.get("text"), str)):
                text = text.replace(entity["text"], "")
        text = html.unescape(text).strip()
        key = "teams:" + hashlib.sha256(json.dumps([config.tenant_id, config.app_id, conversation_id, eid]).encode()).hexdigest()
        payload = {"channel": config.destination, "thread": row["id"], "person": person_id, "oid": oid,
                   "tenant": config.tenant_id, "app": config.app_id, "text": text,
                   "occurrence": {"platform": "teams", "id": eid, "timestamp": activity["timestamp"],
                                  "reply_to": activity.get("replyToId", "")}}
        encoded = json.dumps(payload, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.graph.transaction():
            old = self.graph.db.execute("SELECT digest FROM teams_ingress WHERE id=?", (key,)).fetchone()
            if old and old["digest"] != digest:
                raise TeamsAuthError("Teams message ID was reused with different content")
            self.graph.db.execute("INSERT INTO teams_ingress(id,digest,payload,thread_ts,created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(id) DO NOTHING", (key, digest, encoded, row["id"], now_iso()))
        self.start()
        return {"ok": True, "queued": True}

    @contextmanager
    def _keep_lease(self, event_id):
        finished = threading.Event()
        def heartbeat():
            try:
                while not finished.wait(30):
                    with self.graph.transaction():
                        self.graph.db.execute("UPDATE teams_ingress SET lease_until=? WHERE id=? AND state='processing'",
                                              (time.time() + 120, event_id))
            finally:
                self.graph.close_thread()
        worker = threading.Thread(target=heartbeat, name="raven-teams-lease", daemon=True)
        worker.start()
        try:
            yield
        finally:
            finished.set()
            worker.join(timeout=1)

    def process(self, limit=10):
        done = 0
        with self.graph.transaction():
            # Never automatically rerun an interrupted mutation whose outcome is
            # unknown. Applied receipts can be recovered without invoking it.
            self.graph.db.execute("UPDATE teams_ingress SET state='queued' WHERE state='processing' AND lease_until<? "
                "AND id IN (SELECT id FROM webhook_receipts WHERE state='applied')", (time.time(),))
            self.graph.db.execute("UPDATE teams_ingress SET state='failed',error='InterruptedOutcomeRequiresReview' "
                "WHERE state='processing' AND lease_until<?", (time.time(),))
        rows = self.graph.db.execute("SELECT id FROM teams_ingress WHERE state='queued' AND next_attempt<=? "
                                     "ORDER BY created_at,id LIMIT ?", (time.time(), limit)).fetchall()
        for candidate in rows:
            with self.graph.transaction():
                row = self.graph.db.execute("SELECT * FROM teams_ingress WHERE id=? AND state='queued' AND next_attempt<=?",
                                            (candidate["id"], time.time())).fetchone()
                if row is None:
                    continue
                if self.graph.db.execute("SELECT 1 FROM teams_ingress WHERE thread_ts=? AND state IN ('queued','failed','processing') "
                    "AND (created_at<? OR (created_at=? AND id<?)) LIMIT 1",
                    (row["thread_ts"], row["created_at"], row["created_at"], row["id"])).fetchone():
                    continue
                if self.graph.db.execute("SELECT 1 FROM teams_replies WHERE thread_ts=? AND state!='sent' LIMIT 1",
                                         (row["thread_ts"],)).fetchone():
                    continue
                self.graph.db.execute("UPDATE teams_ingress SET state='processing',lease_until=?,attempts=attempts+1 WHERE id=?",
                                      (time.time() + 120, row["id"]))
            try:
                data = json.loads(row["payload"])
                config = self.delivery.config
                if (data["tenant"] != config.tenant_id or data["app"] != config.app_id
                        or data["channel"] != config.destination or config.people.get(data["oid"]) != data["person"]
                        or not self.delivery.slack_person(data["person"])):
                    raise TeamsAuthError("Teams queued identity is no longer authorized")
                receipt = self.graph.db.execute("SELECT state,reply FROM webhook_receipts WHERE id=?", (row["id"],)).fetchone()
                if receipt is not None:
                    if receipt["state"] != "applied":
                        raise TeamsAuthError("Interrupted Teams action needs operator review")
                    reply = receipt["reply"]
                else:
                    with self._keep_lease(row["id"]):
                        reply = self.delivery.receive(data["channel"], data["thread"], data["person"], data["text"], event_id=row["id"],
                                                      occurrence=data.get("occurrence"))
                with self.graph.transaction():
                    if reply:
                        self.graph.db.execute("INSERT INTO teams_replies(id,channel,thread_ts,text) VALUES(?,?,?,?) "
                            "ON CONFLICT(id) DO NOTHING", (row["id"], data["channel"], data["thread"], str(reply)))
                    self.graph.db.execute("UPDATE teams_ingress SET state='done',payload='{}',error='',lease_until=0 WHERE id=?", (row["id"],))
                done += 1
            except Exception as exc:
                with self.graph.transaction():
                    receipt = self.graph.db.execute("SELECT state FROM webhook_receipts WHERE id=?", (row["id"],)).fetchone()
                    recoverable = receipt is not None and receipt["state"] == "applied" and row["attempts"] + 1 < MAX_ATTEMPTS
                    self.graph.db.execute("UPDATE teams_ingress SET state=?,lease_until=0,next_attempt=?,error=? WHERE id=?",
                        ("queued" if recoverable else "failed", time.time() + 30, type(exc).__name__, row["id"]))
        self.flush()
        return done

    def flush(self):
        rows = self.graph.db.execute("SELECT * FROM teams_replies WHERE state IN ('queued','sending') AND next_attempt<=? ORDER BY id LIMIT 20",
                                     (time.time(),)).fetchall()
        for row in rows:
            with self.graph.transaction():
                claimed = self.graph.db.execute("UPDATE teams_replies SET state='sending',next_attempt=? "
                    "WHERE id=? AND state IN ('queued','sending') AND next_attempt<=?", (time.time() + 120, row["id"], time.time()))
            if not claimed.rowcount:
                continue
            try:
                if hasattr(self.delivery.transport, 'post_reply'):
                    message_ref = self.delivery.transport.post_reply(row['channel'], row['text'], None, row['thread_ts'], row['id'])
                else:
                    message_ref = self.delivery.transport.post_message(row["channel"], row["text"], thread_ts=row["thread_ts"])
                with self.graph.transaction():
                    from .readback import mark_delivered
                    mark_delivered(self.graph, "teams", row["id"], row["channel"], row["thread_ts"], message_ref)
                    self.graph.db.execute("UPDATE teams_replies SET state='sent',error='',attempts=attempts+1 WHERE id=?", (row["id"],))
            except Exception as exc:
                with self.graph.transaction():
                    self.graph.db.execute("UPDATE teams_replies SET state='queued',attempts=attempts+1,next_attempt=?,error=? WHERE id=?",
                        (time.time() + retry_delay(exc, min(3600, 30 * 2 ** min(row["attempts"], 7))), type(exc).__name__, row["id"]))

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            def worker():
                try:
                    while not self.delivery._stop.is_set():
                        try:
                            self.process()
                        except Exception as exc:
                            print("Raven Teams inbox:", type(exc).__name__)
                        self.delivery._stop.wait(1)
                finally:
                    self.graph.close_thread()
            self.thread = threading.Thread(target=worker, name="raven-teams-inbox", daemon=True)
            self.thread.start()
