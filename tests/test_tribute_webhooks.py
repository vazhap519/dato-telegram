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
    InvalidTributeEvent, event_key, parse_event, safe_event_record,
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

    def test_sensitive_fields_not_stored(self):
        rec = safe_event_record(EVENT)
        self.assertEqual(rec["details"]["trb_user_id"], "account-54")
        self.assertNotIn("email", rec["details"])
        self.assertNotIn("do-not-retain@example.test", json.dumps(rec))
        self.assertEqual(len(SUBSCRIPTION_EVENTS), 3)


if __name__ == "__main__":
    unittest.main()
