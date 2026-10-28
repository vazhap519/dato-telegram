import base64
import hashlib
import hmac
import json
import unittest
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import patch

from db.tribute_webhooks import (
    InvalidTributeEvent, event_key, parse_event, parse_signed_delivery, safe_event_record,
    verify_signature, SUBSCRIPTION_EVENTS,
)

KEY = "local-example-key-not-production"
EVENT = {
    "name": "new_subscription",
    "created_at": "2026-10-28T11:00:00Z",
    "sent_at": "2026-10-28T11:00:01Z",
    "payload": {
        "subscription_id": 101,
        "expires_at": "2026-11-28T11:00:00Z",
        "trb_user_id": "account-54",
        "email": "do-not-retain@example.test",
    },
}


class TributeWebhookSafetyTests(unittest.TestCase):
    def test_valid_raw_body_signature(self):
        body = json.dumps(EVENT).encode()
        digest = hmac.new(KEY.encode(), body, hashlib.sha256).digest()
        self.assertTrue(verify_signature(body, digest.hex(), KEY))
        self.assertTrue(verify_signature(body, "sha256=" + digest.hex(), KEY))
        self.assertTrue(verify_signature(body, base64.b64encode(digest).decode(), KEY))

    def test_bad_signature_rejected(self):
        body = json.dumps(EVENT).encode()
        signed = hmac.new(KEY.encode(), body, hashlib.sha256).hexdigest()
        self.assertFalse(verify_signature(body + b" ", signed, KEY))
        self.assertFalse(verify_signature(body, signed, None))
        self.assertFalse(verify_signature(body, None, KEY))
        self.assertFalse(verify_signature(body, "deadbeef", KEY))

    def test_event_validation(self):
        self.assertEqual(parse_event(json.dumps(EVENT).encode()), EVENT)
        for bad in (b"", b"[]", b"{oops", b"{}", b"{}" * 40000):
            with self.subTest(bad=bad[:10]), self.assertRaises(InvalidTributeEvent):
                parse_event(bad)
        invalid = dict(EVENT, created_at="2026-10-28")
        with self.assertRaises(InvalidTributeEvent):
            parse_event(json.dumps(invalid).encode())

    def test_signed_connection_check_without_subscription_envelope(self):
        probe_body = json.dumps({
            "test": True,
            "request_id": "request-123",
            "email": "private@example.test",
        }).encode("utf-8")
        probe = parse_signed_delivery(probe_body)
        self.assertEqual(probe["name"], "tribute_connection_test")
        self.assertEqual(probe["_envelope_fields"], ["email", "request_id", "test"])
        self.assertEqual(probe["payload"].keys(), {"probe_fingerprint"})
        self.assertNotIn("private@example.test", json.dumps(safe_event_record(probe)))
        self.assertEqual(event_key(probe), event_key(parse_signed_delivery(probe_body)))

    def test_signed_event_without_timestamp_is_not_accepted_as_subscription(self):
        malformed = {
            "name": "new_subscription",
            "payload": {"telegram_user_id": 12345},
        }
        with self.assertRaises(InvalidTributeEvent):
            parse_signed_delivery(json.dumps(malformed).encode("utf-8"))

    def test_standard_event_is_unchanged(self):
        self.assertEqual(parse_signed_delivery(json.dumps(EVENT).encode()), EVENT)

    def test_signed_malformed_json_still_rejected(self):
        for body in (b"not json", b"[]", b""):
            with self.subTest(body=body), self.assertRaises(InvalidTributeEvent):
                parse_signed_delivery(body)

    def test_retry_uses_stable_event_key(self):
        retry = dict(EVENT, sent_at="2026-10-28T11:30:00Z")
        self.assertEqual(event_key(EVENT), event_key(retry))
        renewal = dict(EVENT, name="renewed_subscription")
        self.assertNotEqual(event_key(EVENT), event_key(renewal))

    def test_database_inbox_is_idempotent_and_has_no_access_mutations(self):
        from db.tribute_webhooks import store_event

        class Result:
            def __init__(self, value):
                self.value = value

            def scalar_one_or_none(self):
                return self.value

        class Session:
            keys = set()
            sql_statements = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def begin(self):
                return self

            async def execute(self, statement, params):
                self.sql_statements.append(str(statement))
                key = params["event_key"]
                if key in self.keys:
                    return Result(None)
                self.keys.add(key)
                return Result(1)

        with patch.dict(sys.modules, {"db.database": SimpleNamespace(SessionLocal=Session)}):
            self.assertTrue(asyncio.run(store_event(EVENT)))
            retry = dict(EVENT, sent_at="2026-10-28T11:30:00Z")
            self.assertFalse(asyncio.run(store_event(retry)))
        self.assertEqual(len(Session.keys), 1)
        self.assertTrue(all("payment_webhook_events" in sql for sql in Session.sql_statements))
        self.assertFalse(any("access_grants" in sql or "UPDATE subscriptions" in sql for sql in Session.sql_statements))

    def test_known_test_plan_ids_are_visible_without_secrets(self):
        event = dict(EVENT, payload={
            "subscription_id": 279302,
            "period_id": 528940,
            "expires_at": "2026-11-28T17:00:00Z",
            "telegram_user_id": 123456789,
            "credit_card": "never-store-this",
            "email": "do-not-log@example.test",
        })
        record = safe_event_record(event)
        self.assertEqual(record["details"]["subscription_id"], 279302)
        self.assertEqual(record["details"]["period_id"], 528940)
        self.assertEqual(record["details"]["telegram_user_id"], 123456789)
        self.assertEqual(record["details"]["expires_at"], "2026-11-28T17:00:00Z")
        self.assertNotIn("credit_card", record["details"])
        self.assertNotIn("email", record["details"])
        self.assertNotIn("never-store-this", json.dumps(record))
        self.assertNotIn("do-not-log@example.test", json.dumps(record))

    def test_sensitive_fields_not_stored(self):
        rec = safe_event_record(EVENT)
        self.assertEqual(rec["details"]["trb_user_id"], "account-54")
        self.assertNotIn("email", rec["details"])
        self.assertNotIn("do-not-retain@example.test", json.dumps(rec))
        self.assertEqual(len(SUBSCRIPTION_EVENTS), 3)


if __name__ == "__main__":
    unittest.main()
