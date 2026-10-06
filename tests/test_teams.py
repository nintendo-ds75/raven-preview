"""Offline signed Bot Connector fixtures; these are not a live Teams certification."""
import copy
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fixtures import OfflineCase, ready_server
from bridge import canvas
from bridge.config import Config
from bridge.store import Invalid, Store
from bridge.teams import (ConnectorAuth, ISSUER, KEYS_URL, METADATA_URL, TeamsAuthError,
                          TeamsBotTransport, TeamsConfig, TeamsDelivery, TeamsUnavailable)
try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None

APP = "11111111-1111-4111-8111-111111111111"
TENANT = "22222222-2222-4222-8222-222222222222"
OWNER = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
CHANNEL = "19:test-channel@thread.tacv2"
CFG = Config(model_api="none")
@lru_cache(maxsize=1)
def fixture_key():
    """Ephemeral process-local RSA key; no private-key material in source."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class MicrosoftFixture:
    def __init__(self):
        key = fixture_key()
        self.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        self.jwk.update(kid="fixture-key", use="sig", alg="RS256", endorsements=["msteams"])
        self.requests, self.messages = [], []
        self.fail = False
        self.fail_keys = False

    def __call__(self, request):
        self.requests.append(request)
        if request.full_url == METADATA_URL:
            if self.fail_keys:
                raise TeamsUnavailable("fixture metadata unavailable")
            return {"issuer": ISSUER, "jwks_uri": KEYS_URL, "id_token_signing_alg_values_supported": ["RS256"]}
        if request.full_url == KEYS_URL:
            return {"keys": [copy.deepcopy(self.jwk)]}
        if "login.microsoftonline.com" in request.full_url:
            return {"access_token": "fixture-outbound-token", "token_type": "Bearer", "expires_in": 3600}
        if self.fail:
            raise TeamsUnavailable("fixture connector unavailable")
        body = json.loads(request.data)
        self.messages.append(body)
        if request.full_url.endswith("/v3/conversations"):
            n = len(self.messages)
            return {"id": f"19:opaque-conversation:{n};messageid=opaque-root-{n}", "activityId": f"opaque-root-{n}"}
        return {"id": "opaque-reply"}


@unittest.skipUnless(jwt, "Install requirements-teams.txt to test verified Teams replies")
class TeamsTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.store = Store(Path(self.temp.name) / "teams.db")
        self.graph = self.store.graph
        with self.graph.transaction():
            self.wes = self.graph.add_person("Wes Chen", email="wes@example.test")
            self.other = self.graph.add_person("Other Person", email="other@example.test")
            self.graph.add_authority("path", "billing/*", "decides", person_id=self.wes)
        self.config = TeamsConfig(APP, TENANT, CHANNEL, "fixture-secret", {OWNER: self.wes, OTHER: self.other})
        self.microsoft = MicrosoftFixture()
        self.transport = TeamsBotTransport(self.graph, self.config, http=self.microsoft)
        self.auth = ConnectorAuth(self.config, http=self.microsoft)
        self.delivery = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.store._delivery = self.delivery
        self.no_worker = patch.object(self.delivery.inbox, "start")
        self.no_worker.start()
        self.addCleanup(self.no_worker.stop)
        self.addCleanup(self.delivery.close)
        self.task = canvas.start_task(self.store, CFG, {"title": "Usage billing", "repo": "acme/platform",
                                                     "paths": "billing/usage.py"})["task_id"]
        self.node = canvas.add_node(self.store, CFG, {"task_id": self.task, "question": "Bill the usage spike?",
            "paths": "billing/usage.py", "context": "Enterprise-two load test", "options": "Bill | Exclude"})["node_id"]
        self.assertEqual(self.delivery.deliver_now(), 1)
        self.thread = dict(self.graph.db.execute("SELECT * FROM teams_threads").fetchone())
        self.counter = 0

    def activity(self, text="answer: Exclude it because it was a load test", oid=OWNER):
        self.counter += 1
        return {"type": "message", "id": f"activity-{self.counter}", "channelId": "msteams",
            "serviceUrl": self.config.service_url, "timestamp": datetime.now(timezone.utc).isoformat(),
            "conversation": {"id": self.thread["conversation_id"], "conversationType": "channel", "tenantId": TENANT},
            "channelData": {"tenant": {"id": TENANT}, "channel": {"id": CHANNEL}},
            "from": {"id": "29:opaque-user", "aadObjectId": oid, "name": "untrusted display name"},
            "recipient": {"id": self.config.bot_id}, "text": text}

    def token(self, **changes):
        claims = {"iss": ISSUER, "aud": APP, "nbf": int(time.time()) - 30, "exp": int(time.time()) + 300,
                  "serviceurl": self.config.service_url}
        claims.update(changes)
        return "Bearer " + jwt.encode(claims, fixture_key(), algorithm="RS256", headers={"kid": "fixture-key"})

    def submit(self, activity=None):
        activity = activity or self.activity()
        result = self.delivery.handle(self.token(), activity)
        self.delivery.inbox.process()
        return result

    def decision(self):
        return self.store.get_decision(self.node)

    def test_default_owner_destination_preserves_namesake_identity(self):
        self.graph.add_person("Wes Chen", email="other@example.test")
        notice = self.store.notify(self.node, "reassigned")
        self.assertIsNotNone(notice)
        self.assertEqual((notice["destination"], notice["person_id"]), (self.config.destination, self.wes))
        self.assertEqual(self.delivery.deliver_now(), 1)
        # Both namesakes are mapped Teams members; the other still cannot answer.
        self.submit(self.activity(oid=OTHER))
        self.assertEqual(self.decision()["status"], "pending")

    def test_explicit_recipient_keeps_its_identity_instead_of_owner(self):
        notice = self.store.notify(self.node, "signoff", to="Other Person")
        self.assertIsNotNone(notice)
        self.assertEqual((notice["destination"], notice["person_id"]), (self.config.destination, self.other))

    def test_exact_destination_does_not_fall_back_for_missing_or_inactive_person(self):
        self.graph.add_person("Wes Chen", email="other@example.test")
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (self.wes,))
        self.graph._bump("")
        for target in (self.wes, "missing-person-id"):
            with self.subTest(target=target):
                self.assertEqual(self.delivery._destination("Wes Chen", target),
                                 (self.config.destination, "", "", "channel"))

    def test_verified_reply_records_stable_person_and_teams_origin(self):
        self.submit()
        decision = self.decision()
        self.assertEqual((decision["answer"], decision["answered_by"]), ("Exclude it", "Wes Chen"))
        events = [r["detail"] for r in self.graph.db.execute("SELECT detail FROM events WHERE kind='owner_approved'")]
        self.assertTrue(any('teams: Wes Chen' in e for e in events))
        self.assertEqual(self.graph.db.execute("SELECT state FROM teams_replies").fetchone()[0], "sent")
        request = self.microsoft.requests[-1]
        self.assertIn("19%3Aopaque-conversation%3A1%3Bmessageid%3Dopaque-root-1", request.full_url)
        self.assertEqual(request.headers["Authorization"], "Bearer fixture-outbound-token")

    def test_valid_jwt_bad_claim_matrix_changes_nothing(self):
        for claim in ({"aud": "different-app"}, {"aud": [APP]}, {"iss": "https://attacker.test"},
                      {"exp": time.time()-120}, {"nbf": time.time()+120},
                      {"exp": "9999999999"}, {"serviceurl": "https://attacker.test/"}):
            with self.subTest(claim=claim), self.assertRaises(TeamsAuthError):
                self.delivery.handle(self.token(**claim), self.activity())
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_ingress").fetchone()[0], 0)

    def test_missing_claim_unsigned_forged_and_wrong_algorithm_rejected(self):
        token = self.token().split()[1]
        header, body, signature = token.split(".")
        variants = ["", "Basic " + token, "Bearer " + header + "." + body + "." + ("A" if signature[0] != "A" else "B") + signature[1:],
            "Bearer " + jwt.encode({"aud": APP}, "attacker-shared-secret-not-an-rsa-key", algorithm="HS256", headers={"kid":"fixture-key"}),
            "Bearer " + jwt.encode({"aud": APP}, fixture_key(), algorithm="RS256", headers={"kid":"fixture-key"})]
        for value in variants:
            with self.subTest(value=value[:20]), self.assertRaises(TeamsAuthError):
                self.delivery.handle(value, self.activity())
        self.assertEqual(self.decision()["status"], "pending")

    def test_missing_teams_key_endorsement_is_rejected(self):
        self.microsoft.jwk["endorsements"] = ["webchat"]
        with self.assertRaises(TeamsAuthError):
            self.delivery.handle(self.token(), self.activity())

    def test_key_rotation_refresh_and_metadata_fail_closed(self):
        self.auth.verify(self.token(), self.activity())
        self.microsoft.jwk["kid"] = "rotated-key"
        claims = jwt.decode(self.token().split()[1], options={"verify_signature": False})
        rotated = "Bearer " + jwt.encode(claims, fixture_key(), algorithm="RS256", headers={"kid":"rotated-key"})
        self.auth.refreshed -= 61
        self.auth.verify(rotated, self.activity())
        self.microsoft.fail_keys = True
        self.auth.refreshed -= 3601
        with self.assertRaises(TeamsUnavailable):
            self.auth.verify(rotated, self.activity())

    def test_cross_tenant_channel_bot_and_service_url_rejected(self):
        mutations = [lambda a: a["channelData"]["tenant"].update(id=OTHER),
            lambda a: a["conversation"].update(tenantId=OTHER),
            lambda a: a["channelData"]["channel"].update(id="another-channel"),
            lambda a: a.update(channelId="webchat"),
            lambda a: a["recipient"].update(id="28:another-bot"),
            lambda a: a.update(serviceUrl="https://attacker.test/"),
            lambda a: a["conversation"].update(conversationType="personal")]
        for mutate in mutations:
            activity = self.activity()
            mutate(activity)
            with self.assertRaises(TeamsAuthError):
                self.delivery.handle(self.token(), activity)
        self.assertEqual(self.decision()["status"], "pending")

    def test_display_name_and_payload_person_id_cannot_impersonate(self):
        activity = self.activity(oid="55555555-5555-4555-8555-555555555555")
        activity["from"].update(name="Wes Chen", id=self.wes)
        with self.assertRaises(TeamsAuthError):
            self.submit(activity)
        self.assertEqual(self.decision()["status"], "pending")

    def test_mapped_member_without_authority_cannot_answer(self):
        self.submit(self.activity(oid=OTHER))
        self.assertEqual(self.decision()["status"], "pending")
        self.assertIn("Not recorded", self.graph.db.execute("SELECT text FROM teams_replies").fetchone()[0])

    def test_timestamp_window_and_unknown_conversation(self):
        for stamp in ("not-a-date", "2001-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00", "2026-01-01T00:00:00"):
            activity = self.activity(); activity["timestamp"] = stamp
            with self.assertRaises(TeamsAuthError):
                self.delivery.handle(self.token(), activity)
        activity = self.activity(); activity["conversation"]["id"] = "different-thread"
        self.assertEqual(self.submit(activity)["ignored"], "unknown_thread")
        self.assertEqual(self.decision()["status"], "pending")

    def test_duplicate_and_altered_replay_do_not_apply_twice(self):
        activity = self.activity()
        self.submit(activity)
        events_before = self.graph.db.execute("SELECT count(*) FROM events").fetchone()[0]
        self.submit(activity)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events").fetchone()[0], events_before)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_replies").fetchone()[0], 1)
        activity["text"] = "answer: Bill it"
        with self.assertRaises(TeamsAuthError):
            self.submit(activity)
        self.assertEqual(self.decision()["answer"], "Exclude it")

    def test_bot_mention_removed_without_changing_actor(self):
        activity = self.activity("<at>Raven</at> answer: Exclude it because testing")
        activity["entities"] = [{"type":"mention", "text":"<at>Raven</at>", "mentioned":{"id":self.config.bot_id}}]
        self.submit(activity)
        self.assertEqual(self.decision()["answer"], "Exclude it")

    def test_natural_language_readback_then_confirmation(self):
        read = {"kind":"answer", "answer":"Exclude the load test", "rationale":"it was ours", "to":""}
        with patch("bridge.llm.read_reply", return_value=read):
            self.submit(self.activity("We should leave that one out"))
        self.assertEqual(self.decision()["status"], "pending")
        prompt = self.graph.db.execute("SELECT text FROM teams_replies").fetchone()[0]
        self.assertIn("Not recorded yet", prompt)
        held = self.delivery._reading(self.config.destination, self.thread["id"], self.wes)
        self.submit(self.activity("confirm " + held["proposal_id"]))
        self.assertEqual(self.decision()["answer"], "Exclude the load test")

    def test_stale_readback_cannot_sign_new_question(self):
        read = {"kind":"answer", "answer":"Exclude the load test", "rationale":"", "to":""}
        with patch("bridge.llm.read_reply", return_value=read):
            self.submit(self.activity("Leave that one out"))
        with self.graph.transaction():
            self.graph.db.execute("UPDATE decisions SET question='Different question?' WHERE id=?", (self.node,))
        self.submit(self.activity("yes"))
        self.assertEqual(self.decision()["status"], "pending")

    def test_failed_ack_is_visible_and_retried_without_reapplying_answer(self):
        self.microsoft.fail = True
        activity = self.activity()
        self.submit(activity)
        self.assertEqual(self.decision()["answer"], "Exclude it")
        self.assertTrue(self.delivery.reply_failures())
        self.assertEqual(self.graph.db.execute("SELECT state FROM teams_ingress").fetchone()[0], "done")
        self.microsoft.fail = False
        self.graph.db.execute("UPDATE teams_replies SET next_attempt=0")
        self.delivery.inbox.flush()
        self.assertEqual(self.delivery.reply_failures(), [])
        self.submit(activity)
        self.assertEqual(len(self.microsoft.messages), 2)

    def test_crash_after_applied_receipt_recovers_ack_without_reapplying(self):
        original = self.delivery.receive
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated crash before durable ack")
        with patch.object(self.delivery, "receive", side_effect=crash):
            self.submit()
        self.assertEqual(self.decision()["answer"], "Exclude it")
        self.assertEqual(self.graph.db.execute("SELECT state FROM webhook_receipts").fetchone()[0], "applied")
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_replies").fetchone()[0], 0)
        self.assertTrue(self.delivery.inbound_failed())
        self.graph.db.execute("UPDATE teams_ingress SET next_attempt=0")
        self.delivery.inbox.process()
        self.assertEqual(self.decision()["answer"], "Exclude it")

    def test_deactivated_person_is_rechecked_before_queue_replay(self):
        self.delivery.handle(self.token(), self.activity())
        self.graph.db.execute("UPDATE people SET active=0 WHERE id=?", (self.wes,))
        self.delivery.inbox.process()
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(self.delivery.inbound_failed()[0]["error"], "TeamsAuthError")

    def test_explicit_question_only_handoff_retains_scope_rules(self):
        self.submit(self.activity("not me Other Person, this question only"))
        decision = self.decision()
        self.assertEqual(decision["owner_name"], "Other Person")
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM authority WHERE person_id=?", (self.other,)).fetchone()[0], 0)

    def test_opaque_thread_mapping_reused_for_later_notifications(self):
        self.submit()
        self.store.notify(self.node, "signoff", to="Wes Chen")
        self.delivery.deliver_now()
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_threads").fetchone()[0], 1)
        self.assertEqual(sum(r.full_url.endswith("/v3/conversations") for r in self.microsoft.requests), 1)

    def test_http_callback_needs_bot_jwt_not_admin_bearer(self):
        server = ready_server(self.store, port=0, teams_adapter=self.delivery)
        runner = threading.Thread(target=server.serve_forever, daemon=True); runner.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/webhooks/teams"
        for token, expected in (("Bearer admin-or-shared-secret", 403), (self.token(), 200)):
            req = Request(endpoint, data=json.dumps(self.activity()).encode(),
                          headers={"Authorization": token, "Content-Type":"application/json"})
            try:
                with urlopen(req) as resp:
                    status = resp.status
            except HTTPError as exc:
                status = exc.code
            self.assertEqual(status, expected)

    def test_interrupted_unconfirmed_receipt_fails_closed_on_restart(self):
        self.delivery.handle(self.token(), self.activity())
        eid = self.graph.db.execute("SELECT id FROM teams_ingress").fetchone()[0]
        self.graph.db.execute("UPDATE teams_ingress SET state='processing',lease_until=0")
        self.graph.db.execute("INSERT INTO webhook_receipts(id,channel,state,updated_at) VALUES(?,'teams','received','')", (eid,))
        self.delivery.inbox.process()
        self.assertEqual(self.decision()["status"], "pending")
        self.assertEqual(self.delivery.inbound_failed()[0]["error"], "InterruptedOutcomeRequiresReview")
        with self.assertRaisesRegex(Invalid, "outcome is uncertain"):
            self.delivery.retry_inbound(eid)

    def test_applied_receipt_recovered_after_process_restart(self):
        activity = self.activity()
        self.delivery.handle(self.token(), activity)
        row = dict(self.graph.db.execute("SELECT * FROM teams_ingress").fetchone())
        data = json.loads(row["payload"])
        self.delivery.receive(data["channel"], data["thread"], data["person"], data["text"], event_id=row["id"])
        self.graph.db.execute("UPDATE teams_ingress SET state='processing',lease_until=0")
        replacement = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.addCleanup(replacement.close)
        replacement.inbox.process()
        self.assertEqual(self.decision()["answer"], "Exclude it")
        self.assertEqual(self.graph.db.execute("SELECT state FROM teams_replies").fetchone()[0], "sent")
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events WHERE kind='owner_approved'").fetchone()[0], 1)

    def test_failed_readback_delivery_blocks_confirmation_until_sent(self):
        read = {"kind":"answer", "answer":"Exclude the load test", "rationale":"", "to":""}
        self.microsoft.fail = True
        with patch("bridge.llm.read_reply", return_value=read):
            self.submit(self.activity("Leave that one out"))
        self.submit(self.activity("yes"))
        self.assertEqual(self.decision()["status"], "pending")
        self.microsoft.fail = False
        self.graph.db.execute("UPDATE teams_replies SET next_attempt=0")
        self.delivery.inbox.flush()
        self.delivery.inbox.process()
        self.assertEqual(self.decision()["status"], "pending")
        held = self.delivery._reading(self.config.destination, self.thread["id"], self.wes)
        self.submit(self.activity("confirm " + held["proposal_id"]))
        self.assertEqual(self.decision()["answer"], "Exclude the load test")

    def test_concurrent_duplicate_callback_applies_once(self):
        activity = self.activity()
        self.delivery.handle(self.token(), activity)
        errors = []
        def run():
            try:
                self.delivery.inbox.process()
            except Exception as exc:
                errors.append(exc)
            finally:
                self.graph.close_thread()
        workers = [threading.Thread(target=run) for _ in range(2)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(5)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertFalse(errors)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM events WHERE kind='owner_approved'").fetchone()[0], 1)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_replies").fetchone()[0], 1)

    def test_malformed_sender_context_never_enqueues(self):
        for value in ([], {}, 12, None):
            activity = self.activity(); activity["from"]["aadObjectId"] = value
            with self.assertRaises(TeamsAuthError):
                self.delivery.handle(self.token(), activity)
        self.assertEqual(self.graph.db.execute("SELECT count(*) FROM teams_ingress").fetchone()[0], 0)

    def test_metadata_cannot_redirect_key_lookup_to_attacker(self):
        def metadata(request):
            self.assertEqual(request.full_url, METADATA_URL)
            return {"issuer":ISSUER, "jwks_uri":"https://attacker.test/keys", "id_token_signing_alg_values_supported":["RS256"]}
        auth = ConnectorAuth(self.config, http=metadata)
        with self.assertRaises(TeamsUnavailable):
            auth.verify(self.token(), self.activity())

    def test_jwt_header_cannot_replace_the_trusted_key_source(self):
        claims = jwt.decode(self.token().split()[1], options={"verify_signature":False})
        token = "Bearer " + jwt.encode(claims, fixture_key(), algorithm="RS256",
            headers={"kid":"fixture-key", "jku":"https://attacker.test/jwks", "jwk":{"kty":"oct","k":"attacker"}})
        self.auth.verify(token, self.activity())
        self.assertFalse(any("attacker.test" in request.full_url for request in self.microsoft.requests))

    def test_bot_oauth_token_is_cached_and_bound_to_single_tenant(self):
        self.submit()
        requests = [r for r in self.microsoft.requests if "login.microsoftonline.com" in r.full_url]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token")
        self.assertIn("scope=https%3A%2F%2Fapi.botframework.com%2F.default", requests[0].data.decode())
        self.assertNotIn("fixture-secret", repr(self.config))

    def test_outbound_wrong_destination_and_missing_resource_id_fail(self):
        with self.assertRaises(Invalid):
            self.transport.post_message("attacker-channel", "secret")
        with self.assertRaises(Invalid):
            self.transport.post_message(self.config.destination, "secret", thread_ts="invented")
        with patch.object(self.transport, "http", return_value={"id":"conversation-without-activity"}), self.assertRaises(TeamsUnavailable):
            self.transport.post_message(self.config.destination, "message")

    def test_rate_limit_and_expired_oauth_token_remain_retryable(self):
        self.microsoft.fail = True
        self.submit()
        with patch.object(self.transport, "post_reply", side_effect=TeamsUnavailable("throttled", status=429, retry_after=7200)):
            self.graph.db.execute("UPDATE teams_replies SET next_attempt=0")
            self.delivery.inbox.flush()
        delay = self.graph.db.execute("SELECT next_attempt FROM teams_replies").fetchone()[0] - time.time()
        self.assertGreater(delay, 7190)
        with patch.object(self.transport, "http", side_effect=TeamsUnavailable("unauthorized", status=401)), self.assertRaises(TeamsUnavailable):
            self.transport.post_message(self.config.destination, "retry", thread_ts=self.thread["id"])
        self.assertEqual(self.transport.token, "")

    def test_independent_stdio_store_enqueues_without_credentials(self):
        independent = Store(self.store.path)
        self.addCleanup(independent.graph.close)
        with patch.dict("os.environ", {}, clear=True), patch("bridge.teams.ConnectorAuth", side_effect=AssertionError("stdio tried authentication")):
            delivery = independent.delivery
            self.assertTrue(delivery.enabled)
            self.assertEqual(delivery.channel, "teams")
            self.assertIsNone(delivery.transport)
            self.assertEqual(delivery.deliver_now(), 0)
            notification = independent.notify(self.node, "review", to="Wes Chen")
        self.assertEqual(notification["channel"], "teams")
        self.assertEqual(notification["destination"], self.config.destination)
        self.assertEqual(notification["state"], "queued")
        metadata = json.loads(self.graph.get_setting("teams_delivery"))
        self.assertEqual(metadata, {"version":1, "mode":"bot", "destination":self.config.destination})
        self.assertNotIn(self.config.client_secret, json.dumps(metadata))
        self.assertNotIn(self.config.service_url, json.dumps(metadata))
        self.assertNotIn(OWNER, json.dumps(metadata))
        # A replacement configured server can drain the shared durable outbox.
        restarted = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.deliver_now(), 1)
        self.assertEqual(restarted.get(notification["id"])["state"], "sent")

    def test_real_stdio_mcp_process_enqueues_with_jwt_unavailable(self):
        request = {"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{"name":"bridge_add_node", "arguments":{
            "task_id":self.task, "question":"Should the monthly billing ceiling be fixed?", "paths":"billing/usage.py", "options":"Fixed | Flexible"}}}
        env = {key:value for key,value in os.environ.items() if not key.startswith(("TEAMS_", "SLACK_"))}
        env["BRIDGE_MODEL_API"] = "none"
        script = "import sys; sys.modules['jwt'] = None; from bridge.__main__ import main; main()"
        result = subprocess.run([sys.executable, "-c", script, "mcp", "--db", str(self.store.path)],
            cwd=Path(__file__).resolve().parents[1], env=env, input=json.dumps(request)+"\n",
            capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = next(json.loads(line) for line in result.stdout.splitlines() if json.loads(line).get("id") == 2)
        self.assertFalse(response["result"]["isError"], response)
        node = json.loads(response["result"]["content"][0]["text"])["node_id"]
        notification = self.graph.db.execute("SELECT * FROM notifications WHERE decision_id=?", (node,)).fetchone()
        self.assertIsNotNone(notification)
        self.assertEqual((notification["channel"], notification["destination"], notification["state"]),
                         ("teams", self.config.destination, "queued"))
        self.assertEqual(self.delivery.deliver_now(), 1)

    def test_running_stdio_sees_disabled_or_switched_delivery(self):
        independent = Store(self.store.path)
        self.addCleanup(independent.graph.close)
        delivery = independent.delivery
        self.assertTrue(delivery.enabled)
        self.graph.set_setting("teams_delivery", "")
        self.assertFalse(delivery.enabled)
        self.assertIsNone(independent.notify(self.node, "review"))
        # Publishing again does not require restarting this stdio process.
        replacement = TeamsDelivery(self.store, self.config, self.transport, self.auth)
        self.addCleanup(replacement.close)
        self.assertEqual(delivery.channel, "teams")
        class Slack:
            name = "slack"
        self.store.connect_delivery(Slack())
        self.assertEqual(self.graph.get_setting("teams_delivery"), "")
        self.assertEqual(delivery.channel, "slack")

    def test_malformed_persisted_destination_is_not_an_endpoint_override(self):
        independent = Store(self.store.path)
        self.addCleanup(independent.graph.close)
        for metadata in ("bad-json", "[]", json.dumps({"version":1, "mode":"bot", "destination":"https://evil.test/"}),
                         json.dumps({"version":2, "mode":"bot", "destination":self.config.destination})):
            self.graph.set_setting("teams_delivery", metadata)
            self.assertFalse(independent.delivery.enabled)
            self.assertEqual(independent.delivery.deliver_now(), 0)

    def test_endpoint_configuration_refuses_credential_exfiltration_routes(self):
        endpoints = ("http://smba.trafficmanager.net/teams/", "https://smba.trafficmanager.net.evil.test/teams/",
            "https://fixture@example.invalid@smba.trafficmanager.net/teams/", "https://smba.trafficmanager.net:444/teams/",
            "https://127.0.0.1/teams/", "https://smba.trafficmanager.net/teams/?target=evil",
            "https://smba.trafficmanager.net/teams/#fragment", "https://smba.trafficmanager.net/../evil/")
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(Invalid):
                TeamsConfig(APP, TENANT, CHANNEL, "secret", {OWNER:self.wes}, endpoint)
        from bridge.teams import _NoRedirect
        with self.assertRaises(TeamsUnavailable):
            _NoRedirect().redirect_request(Request(self.config.service_url), None, 307, "redirect", {}, "https://evil.test/")

    def test_no_config_or_partial_config_never_fetches_keys(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(TeamsConfig.from_env())
        with patch.dict("os.environ", {"TEAMS_APP_ID": APP}, clear=True), self.assertRaises(Invalid):
            TeamsConfig.from_env()
        with self.assertRaises(Invalid):
            TeamsConfig(APP, TENANT, CHANNEL, "secret", {OWNER:self.wes}, "https://evil.test/teams/")


if __name__ == "__main__":
    unittest.main()
