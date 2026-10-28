"""Tribute automation safety tests; all examples are fake and offline."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from db.tribute_auto import (
    AutoPaymentError, CYCLES, TEST_CHANNEL_ID, TEST_PLAN_CODE,
    validate_paid_event, pending_auto_event_ids, is_tribute_checkout,
)

WHEN = datetime(2026,10,28,18,0,tzinfo=timezone.utc)


def offer(**updates):
    result = {
        "subscription_id":279302,"period_id":528940,
        "channel_id":TEST_CHANNEL_ID,"amount_minor":100,
        "currency":"usd","period":"monthly",
        "plan_id":4,"require_test_claim":True
    }
    result.update(updates)
    return result


def event(name="new_subscription", **updates):
    details = {
        "subscription_id":279302,"period_id":528940,
        "channel_id":TEST_CHANNEL_ID,"amount":100,
        "currency":"usd","period":"monthly","type":"regular",
        "trb_user_id":"T-59727293","telegram_user_id":8609932510,
        # Deprecated Tribute user_id is DELIBERATELY absent.
        "expires_at":"2026-11-28T17:44:35.504330337Z",
    }
    details.update(updates)
    return {
        "name":name,
        "created_at":"2026-10-28T17:44:35.531648Z",
        "details":details,
    }


class TributeAutoValidationTests(unittest.TestCase):
    def test_live_payment_shape_without_deprecated_user_id(self):
        result = validate_paid_event(event(),offer(),now=WHEN)
        self.assertEqual(result["telegram_id"],8609932510)
        self.assertEqual(result["trb_user_id"],"T-59727293")
        self.assertEqual(result["price"],Decimal("1"))
        self.assertEqual(result["expires_at"].day,28)
        self.assertEqual(result["expires_at"].month,11)

    def test_renewal_of_same_buyer_without_deprecated_user_id(self):
        result = validate_paid_event(
            event("renewed_subscription"),
            offer(),now=WHEN
        )
        self.assertEqual(result["event_name"],"renewed_subscription")

    def test_unrelated_existing_offers_cannot_match_test_offer(self):
        for id in (229558,183308,236047):
            with self.subTest(offer_id=id),self.assertRaises(AutoPaymentError):
                validate_paid_event(event(subscription_id=id),offer(),now=WHEN)

    def test_price_channel_period_currency_must_match(self):
        mismatches = [
            ("period_id",0),("channel_id",999999),("amount",1900),
            ("currency","eur"),("period","yearly"),("type","trial"),
        ]
        for field,value in mismatches:
            with self.subTest(field=field), self.assertRaises(AutoPaymentError):
                validate_paid_event(event(**{field:value}),offer(),now=WHEN)

    def test_numeric_strings_floats_and_bool_are_rejected(self):
        for field in ("subscription_id","period_id","channel_id",
                      "amount","telegram_user_id"):
            for value in (True,1.0,"1"):
                with self.subTest(field=field,value=value),self.assertRaises(AutoPaymentError):
                    validate_paid_event(event(**{field:value}),offer(),now=WHEN)

    def test_buyer_identity_must_be_valid_not_guessed(self):
        for value in ("","name@example.com",True,object(),"T-invalid space"):
            with self.subTest(value=str(value)),self.assertRaises(AutoPaymentError):
                validate_paid_event(event(trb_user_id=value),offer(),now=WHEN)
        # New unified IDs are accepted without an old Tribute user_id.
        result = validate_paid_event(event(trb_user_id="U-new-account_99"),offer(),now=WHEN)
        self.assertEqual(result["trb_user_id"],"U-new-account_99")

    def test_no_cancel_or_gift_provides_paid_access(self):
        for name in ("cancelled_subscription","new_donation","integration_probe"):
            with self.subTest(name=name),self.assertRaises(AutoPaymentError):
                validate_paid_event(event(name),offer(),now=WHEN)
        with self.assertRaises(AutoPaymentError):
            validate_paid_event(event(type="gift"),offer(),now=WHEN)

    def test_expired_and_future_events_rejected(self):
        with self.assertRaises(AutoPaymentError):
            validate_paid_event(event(),offer(),now=datetime(2026,12,1,tzinfo=timezone.utc))
        with self.assertRaises(AutoPaymentError):
            validate_paid_event(event(),offer(),now=datetime(2026,10,1,tzinfo=timezone.utc))

    def test_wrong_subscription_length_rejected(self):
        for expiry in ("2026-10-29T17:44:35Z","2027-10-28T17:44:35Z"):
            with self.subTest(expiry=expiry),self.assertRaises(AutoPaymentError):
                validate_paid_event(event(expires_at=expiry),offer(),now=WHEN)

    def test_all_supported_cycle_windows_are_bounded(self):
        for period,(min_days,max_days,plan_days) in CYCLES.items():
            started = datetime(2026,10,28,17,44,tzinfo=timezone.utc)
            exp = started + timedelta(days=plan_days)
            payload = event(period=period,expires_at=exp.isoformat())
            event_record = dict(payload,created_at=started.isoformat())
            matching_offer = offer(period=period)
            result = validate_paid_event(event_record,matching_offer,now=WHEN)
            self.assertEqual(result["expires_at"],exp)
            self.assertLessEqual(min_days,plan_days)
            self.assertLessEqual(plan_days,max_days)

    def test_production_plan_requires_direct_tribute_payment_link(self):
        accepted = [
            "https://t.me/tribute/app?startapp=s1aES",
            "https://web.tribute.tg/s/1aES",
        ]
        denied = [
            "", "http://t.me/tribute/app?startapp=s1aES",
            "https://evil.example/s/1aES",
            "https://web.tribute.tg.evil.example/s/1aES",
            "https://t.me@evil.example/tribute/app?startapp=s1aES",
            "https://t.me/tribute/app",
            "https://t.me/anotherbot?start=123",
            "https://web.tribute.tg/s/a",
            "https://web.tribute.tg/s/1aES?redirect=https://evil.example",
        ]
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(is_tribute_checkout(url))
        for url in denied:
            with self.subTest(url=url):
                self.assertFalse(is_tribute_checkout(url))

    def test_nobody_can_be_granted_access_by_cancellation(self):
        for name in ("cancelled_subscription", "cancelled_donation"):
            with self.subTest(name=name), self.assertRaises(AutoPaymentError):
                validate_paid_event(event(name),offer(),now=WHEN)

    def test_no_automatic_removal_and_flags_off_by_default(self):
        root = Path(__file__).resolve().parents[1]
        code = (root / "db/tribute_auto.py").read_text(encoding="utf-8")
        worker = (root / "bot/tribute_worker.py").read_text(encoding="utf-8")
        self.assertIn("enabled BOOLEAN NOT NULL DEFAULT FALSE",code)
        self.assertIn("require_test_claim BOOLEAN NOT NULL DEFAULT FALSE",code)
        self.assertIn("ON CONFLICT(code) DO NOTHING",code)
        self.assertIn("FALSE,FALSE,''",code)
        self.assertIn("received_at >= o.enabled_at",code)
        self.assertIn("received_at >= a.enabled_at",code)
        self.assertIn("FOR UPDATE",code)
        self.assertIn("GREATEST(paid_until,:expiry)",code)
        self.assertIn("ON CONFLICT(provider,provider_payment_id) DO NOTHING",code)
        self.assertIn("is_auto_processing_enabled()",worker)
        self.assertNotIn("ban_chat_member",code+worker)
        self.assertNotIn("DELETE FROM access_grants",code+worker)
        self.assertNotIn("UPDATE access_grants",code+worker)
        self.assertNotIn("cancelled_subscription",code)

    def test_pending_query_uses_exact_offer_and_post_enable_cutoffs(self):
        class Result:
            def scalars(self):
                return self
            def all(self):
                return [31,32]
        class FakeSession:
            calls=[]
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            async def execute(self,statement,params):
                self.calls.append((str(statement),params))
                return Result()
        with patch.dict(sys.modules, {
            "db.database": SimpleNamespace(SessionLocal=FakeSession)
        }):
            ids = asyncio.run(pending_auto_event_ids())
        self.assertEqual(ids,[31,32])
        sql,params = FakeSession.calls[0]
        self.assertIn("signature_valid=TRUE",sql)
        self.assertIn("status='pending_review'",sql)
        self.assertIn("o.enabled=TRUE AND a.enabled=TRUE",sql)
        self.assertIn("e.received_at >= o.enabled_at",sql)
        self.assertIn("e.received_at >= a.enabled_at",sql)
        self.assertIn("CAST(o.subscription_id AS TEXT)",sql)
        self.assertIn("CAST(o.period_id AS TEXT)",sql)
        self.assertEqual(params["limit"],20)


if __name__ == "__main__":
    unittest.main()
