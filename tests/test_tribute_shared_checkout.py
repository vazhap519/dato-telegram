"""Safety tests for one Telegram bot checkout and four webhook-selected periods."""
import asyncio
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from db.tribute_shared_checkout import (
    CheckoutNotReady, PUBLIC_CHANNEL_ID, PRODUCTION_OFFER_ID, PRODUCTION_URL,
    PERIODS, expected_periods, is_expected_production_offer,
    begin_shared_checkout, set_shared_checkout_enabled,
)


class FakeResult:
    def __init__(self, value):
        self.value=value
    def mappings(self):
        return self
    def one(self):
        return self.value
    def scalar_one_or_none(self):
        return self.value
    def scalar_one(self):
        return self.value


class FakeDb:
    checkout_enabled=True
    global_enabled=True
    complete=True
    accepted=True
    inserts=[]
    statements=[]

    @classmethod
    def reset(cls):
        cls.checkout_enabled=True
        cls.global_enabled=True
        cls.complete=True
        cls.accepted=True
        cls.inserts=[]
        cls.statements=[]

    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    def begin(self):
        return self

    async def execute(self,statement,params=None):
        sql=" ".join(str(statement).split())
        self.statements.append(sql)
        if sql.startswith("SELECT c.subscription_id,c.checkout_url"):
            return FakeResult({
                "subscription_id":PRODUCTION_OFFER_ID,
                "checkout_url":PRODUCTION_URL,
                "checkout_enabled":FakeDb.checkout_enabled,
                "automation_enabled":FakeDb.global_enabled,
            })
        if sql.startswith("SELECT COUNT(*) AS total,COUNT(*) FILTER"):
            n=4 if FakeDb.complete else 2
            return FakeResult({"total":n,"active":n})
        if sql.startswith("SELECT f.offer_version"):
            return FakeResult("1" if FakeDb.accepted else None)
        if sql.startswith("INSERT INTO tribute_checkout_intents"):
            FakeDb.inserts.append(dict(params))
            return FakeResult(None)
        raise AssertionError("Unexpected SQL: "+sql[:120])


class SharedCheckoutTests(unittest.TestCase):
    def setUp(self):
        FakeDb.reset()

    def test_public_offer_periods_and_prices_match_verified_tribute_api(self):
        self.assertEqual(PRODUCTION_OFFER_ID,279348)
        self.assertEqual(PUBLIC_CHANNEL_ID,547415)
        self.assertEqual(PRODUCTION_URL,
                         "https://t.me/tribute/app?startapp=s1aFC")
        self.assertEqual(set(expected_periods()),{529035,529034,529033,529032})
        self.assertEqual([row[2] for row in PERIODS],[1900,4900,9500,17900])
        self.assertEqual([row[1] for row in PERIODS],
                         ["monthly","quarterly","halfyearly","yearly"])

    def test_mapping_only_accepts_public_channel_and_exact_four_prices(self):
        base={"subscription_id":PRODUCTION_OFFER_ID,"period_id":529034,
              "channel_id":PUBLIC_CHANNEL_ID,"amount_minor":4900,
              "currency":"eur","period":"quarterly","require_test_claim":False}
        self.assertTrue(is_expected_production_offer(base))
        for key,bad in (
            ("subscription_id",183308),("period_id",239578),
            ("channel_id",-1002874265969),("amount_minor",1900),
            ("currency","usd"),("period","monthly"),
            ("require_test_claim",True),("period_id","529034"),
            ("amount_minor",4900.0)
        ):
            with self.subTest(key=key,bad=bad):
                self.assertFalse(is_expected_production_offer(dict(base,**{key:bad})))

    def test_single_link_records_only_offer_intent_not_a_chosen_period(self):
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeDb)
        }):
            url=asyncio.run(begin_shared_checkout(123456789))
        self.assertEqual(url,PRODUCTION_URL)
        self.assertEqual(len(FakeDb.inserts),1)
        self.assertEqual(FakeDb.inserts[0]["offer"],PRODUCTION_OFFER_ID)
        self.assertEqual(FakeDb.inserts[0]["uid"],123456789)
        self.assertNotIn("period_id",FakeDb.inserts[0])
        self.assertNotIn("plan_id",FakeDb.inserts[0])

    def test_disabled_automation_checkout_incomplete_periods_or_terms_reject(self):
        for field in ("checkout_enabled","global_enabled","complete","accepted"):
            FakeDb.reset()
            setattr(FakeDb,field,False)
            with self.subTest(field=field):
                with patch.dict(sys.modules,{
                    "db.database":SimpleNamespace(SessionLocal=FakeDb)
                }):
                    self.assertIsNone(asyncio.run(begin_shared_checkout(123456789)))
                self.assertEqual(FakeDb.inserts,[])
        self.assertIsNone(asyncio.run(begin_shared_checkout(-1)))

    def test_no_access_grants_or_group_removals_in_checkout(self):
        root=Path(__file__).resolve().parents[1]
        model=(root/"db/tribute_shared_checkout.py").read_text("utf-8")
        bot=(root/"bot/handlers.py").read_text("utf-8")
        admin=(root/"admin/main.py").read_text("utf-8")
        processing=(root/"db/tribute_auto.py").read_text("utf-8")
        self.assertIn("enabled BOOLEAN NOT NULL DEFAULT FALSE",model)
        self.assertIn("checkout_url",model)
        self.assertIn("tribute_checkout_intents",model)
        self.assertIn("begin_shared_checkout(query.from_user.id)",bot)
        self.assertIn("tribute_checkout_intents i",processing)
        self.assertIn("if offer_id == PRODUCTION_OFFER_ID:",processing)
        self.assertNotIn("DELETE FROM access_grants",model)
        self.assertNotIn("ban_chat_member",model)
        self.assertIn('"/subscriptions/tribute-auto/checkout",',admin)
        self.assertIn("csrf_check=Depends(verify_csrf)",admin)
        self.assertIn("user=Depends(get_current_user)",admin)


if __name__=="__main__":
    unittest.main()
