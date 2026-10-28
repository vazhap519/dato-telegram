"""Offline tests: isolated payment-preview links cannot grant subscriber access."""
import asyncio
import hashlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from db.tribute_payment_test import (
    TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID, TEST_PAYMENT_URL,
    valid_test_token, hash_test_token,
    create_tribute_test_link, claim_tribute_test_link,
)


class TributeTestLinkTests(unittest.TestCase):
    def test_known_test_offer_is_not_an_existing_community_subscription(self):
        self.assertEqual(TEST_SUBSCRIPTION_ID, 279302)
        self.assertEqual(TEST_PERIOD_ID, 528940)
        self.assertEqual(TEST_PAYMENT_URL, "https://t.me/tribute/app?startapp=s1aES")
        self.assertNotIn(TEST_SUBSCRIPTION_ID, {229558, 183308, 236047})

    def test_token_validation_and_hashing(self):
        token = "tt_" + "A" * 32
        self.assertTrue(valid_test_token(token))
        self.assertEqual(hash_test_token(token), hashlib.sha256(token.encode()).hexdigest())
        self.assertFalse(valid_test_token("tt_" + "A" * 31))
        self.assertFalse(valid_test_token("../" + "A" * 32))
        self.assertFalse(valid_test_token("tt_" + "A" * 31 + " "))
        with self.assertRaises(ValueError):
            hash_test_token("invalid")

    def test_one_use_claim_and_no_entitlement_mutations(self):
        class Result:
            def __init__(self, value):
                self.value = value

            def first(self):
                return self.value

        class FakeSession:
            hashes = set()
            statements = []
            claimed_by = {}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def begin(self):
                return self

            async def execute(self, statement, params):
                sql = str(statement).strip()
                self.statements.append(sql)
                if sql.startswith("INSERT INTO tribute_payment_test_links"):
                    self.hashes.add(params["digest"])
                    return Result(None)
                if sql.startswith("UPDATE tribute_payment_test_links"):
                    digest = params["digest"]
                    if digest in self.hashes and digest not in self.claimed_by:
                        self.claimed_by[digest] = params["uid"]
                        return Result((1,))
                    return Result(None)
                raise AssertionError("Unexpected query")

        with patch.dict(sys.modules, {
            "db.database": SimpleNamespace(SessionLocal=FakeSession),
        }):
            with patch(
                "db.tribute_payment_test.secrets.token_urlsafe",
                return_value="A" * 32,
            ):
                token = asyncio.run(create_tribute_test_link("admin"))
            self.assertEqual(token, "tt_" + "A" * 32)
            self.assertTrue(asyncio.run(claim_tribute_test_link(token, 123456789)))
            self.assertFalse(asyncio.run(claim_tribute_test_link(token, 123456789)))
            self.assertFalse(asyncio.run(claim_tribute_test_link(token, 999)))
            self.assertFalse(asyncio.run(claim_tribute_test_link("invalid", 123456789)))

        self.assertEqual(len(FakeSession.hashes), 1)
        self.assertEqual(list(FakeSession.claimed_by.values()), [123456789])
        self.assertFalse(any(
            "access_grants" in sql or "UPDATE subscriptions" in sql
            or "payment_transactions" in sql
            for sql in FakeSession.statements
        ))


if __name__ == "__main__":
    unittest.main()
