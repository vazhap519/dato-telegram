"""Safety checks for approved, signed, narrow-scope Tribute test payments."""
import copy
from datetime import datetime, timezone
from pathlib import Path
import unittest

from db.tribute_test_activation import (
    TributeActivationError, TEST_CHANNEL_ID, TEST_CENTS, TEST_PLAN_CODE,
    validate_test_purchase,
)


def example():
    return {
        "name": "new_subscription",
        "created_at": "2026-10-28T17:44:35.531648Z",
        "details": {
            "subscription_id": 279302,
            "period_id": 528940,
            "channel_id": TEST_CHANNEL_ID,
            "amount": TEST_CENTS,
            "currency": "usd",
            "period": "monthly",
            "type": "regular",
            "user_id": 97,
            "trb_user_id": "T-97",
            "telegram_user_id": 123456789,
            "expires_at": "2026-11-28T17:44:35.504330337Z",
        },
    }


TEST_NOW = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)


class TributeTestActivationTests(unittest.TestCase):
    def test_real_event_shape_with_nanosecond_expiry_accepted(self):
        result = validate_test_purchase(example(), now=TEST_NOW)
        self.assertEqual(result["telegram_id"], 123456789)
        self.assertEqual(result["tribute_user_id"], "T-97")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(str(result["amount"]), "1")
        self.assertEqual(result["paid_until"].year, 2026)
        self.assertEqual(result["paid_until"].month, 11)
        self.assertEqual(result["paid_until"].day, 28)

    def test_different_offers_periods_channels_or_amount_rejected(self):
        fields = [
            ("subscription_id", 229558),
            ("period_id", 419747),
            ("channel_id", 999999),
            ("amount", 1900),
            ("currency", "eur"),
            ("period", "yearly"),
            ("type", "trial"),
        ]
        for field, value in fields:
            with self.subTest(field=field):
                event = example()
                event["details"][field] = value
                with self.assertRaises(TributeActivationError):
                    validate_test_purchase(event, now=TEST_NOW)

    def test_numeric_strings_and_booleans_are_not_accepted_as_integer_ids(self):
        for key in ("subscription_id", "period_id", "channel_id", "amount", "telegram_user_id"):
            for bad in ("123", True, 279302.0):
                with self.subTest(key=key, bad=bad):
                    event = example()
                    event["details"][key] = bad
                    with self.assertRaises(TributeActivationError):
                        validate_test_purchase(event, now=TEST_NOW)

    def test_internal_tribute_id_is_not_telegram_id(self):
        event = example()
        event["details"]["telegram_user_id"] = 987654321
        got = validate_test_purchase(event, now=TEST_NOW)
        self.assertEqual(got["telegram_id"], 987654321)
        self.assertNotEqual(got["telegram_id"], event["details"]["user_id"])

    def test_buyer_id_mismatch_rejected(self):
        for bad in ("T-98", "T-97-other", "", 97, None):
            with self.subTest(bad=bad):
                event = example()
                event["details"]["trb_user_id"] = bad
                with self.assertRaises(TributeActivationError):
                    validate_test_purchase(event, now=TEST_NOW)

    def test_expired_subscription_rejected(self):
        with self.assertRaises(TributeActivationError):
            validate_test_purchase(
                example(), now=datetime(2026, 12, 1, tzinfo=timezone.utc)
            )

    def test_unrealistic_duration_rejected(self):
        for expiry in ("2026-10-29T17:44:35Z", "2027-10-28T17:44:35Z"):
            with self.subTest(expiry=expiry):
                event = example()
                event["details"]["expires_at"] = expiry
                with self.assertRaises(TributeActivationError):
                    validate_test_purchase(event, now=TEST_NOW)

    def test_fake_payment_event_names_rejected(self):
        for name in ("cancelled_subscription", "new_donation", "integration_probe", "renewed_subscription"):
            event = example()
            event["name"] = name
            with self.subTest(name=name), self.assertRaises(TributeActivationError):
                validate_test_purchase(event, now=TEST_NOW)

    def test_missing_buyer_or_expiry_rejected(self):
        for field in ("telegram_user_id", "trb_user_id", "expires_at"):
            event = example()
            del event["details"][field]
            with self.subTest(field=field), self.assertRaises(TributeActivationError):
                validate_test_purchase(event, now=TEST_NOW)

    def test_only_a_hidden_test_plan_is_created(self):
        self.assertEqual(TEST_PLAN_CODE, "tribute_test_279302_monthly")
        source = (Path(__file__).resolve().parents[1] / "db/tribute_test_activation.py").read_text("utf-8")
        self.assertIn("FALSE,FALSE,''", source)
        self.assertIn("signature_valid", source)
        self.assertIn("tribute_payment_test_links", source)
        self.assertIn("FOR UPDATE", source)
        self.assertIn("ON CONFLICT(provider,provider_payment_id) DO NOTHING", source)
        self.assertNotIn("DELETE FROM access_grants", source)
        self.assertNotIn("ban_chat_member", source)


if __name__ == "__main__":
    unittest.main()
