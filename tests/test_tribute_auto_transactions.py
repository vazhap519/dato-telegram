"""Offline integration simulation of Tribute auto purchase and renewal.

An in-memory SQL stub observes the real production transaction's statement
sequence; never contacts Telegram, Tribute or production PostgreSQL.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from db.tribute_auto import activate_auto_event


class Result:
    def __init__(self, value=None):
        self.value=value
    def mappings(self):
        return self
    def first(self):
        return self.value
    def one(self):
        if self.value is None:
            raise AssertionError("Expected one SQL row")
        return self.value
    def scalar_one(self):
        return self.value
    def scalar_one_or_none(self):
        return self.value


class MemoryDB:
    def __init__(self):
        self.now=datetime.now(timezone.utc).replace(microsecond=0)
        self.user=8609932510
        self.plan_price=Decimal("1")
        self.plan_currency="USD"
        self.plan_active=False
        self.plan_url=""
        self.checkout_intent=True
        self.offer_accepted=True
        self.offer={
            "subscription_id":279302,"period_id":528940,
            "channel_id":547415,"amount_minor":100,
            "currency":"usd","period":"monthly",
            "plan_id":51,"enabled":True,
            "enabled_at":self.now-timedelta(hours=1),
            "require_test_claim":True,
        }
        self.sub=None
        self.binding=None
        self.receipts={}
        self.outbox=set()
        self.audit=[]
        self.statements=[]
        self.events={}
        self.add_event(1,"new_subscription",30)

    def add_event(self,event_id,name,expiry_days):
        record={
            "name":name,
            "created_at":(self.now-timedelta(seconds=2)).isoformat(),
            "details":{
                "subscription_id":279302,"period_id":528940,
                "channel_id":547415,"amount":100,
                "currency":"usd","period":"monthly","type":"regular",
                "trb_user_id":"T-59727293",
                "telegram_user_id":self.user,
                "expires_at":(self.now+timedelta(days=expiry_days)).isoformat(),
            }
        }
        self.events[event_id]={
            "id":event_id,"event_key":f"payment-{event_id}",
            "signature_valid":True,"status":"pending_review",
            "payload":record,"received_at":self.now,
        }

    def session_factory(self):
        db=self
        class Session:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):return False
            def begin(self):return self

            async def execute(self,query,params=None):
                sql=" ".join(str(query).split())
                p=params or {}
                db.statements.append(sql)
                if sql.startswith("SELECT id,event_key,signature_valid,status,payload,received_at"):
                    return Result(db.events[p["id"]].copy())
                if sql.startswith("SELECT enabled,enabled_at FROM tribute_auto_settings"):
                    return Result({"enabled":True,"enabled_at":db.now-timedelta(hours=1)})
                if sql.startswith("SELECT * FROM tribute_auto_offers"):
                    return Result(db.offer.copy())
                if sql.startswith("SELECT id,price_amount,currency,is_lifetime"):
                    return Result({"id":db.offer["plan_id"],
                                   "price_amount":db.plan_price,
                                   "currency":db.plan_currency,"is_lifetime":False,
                                   "is_active":db.plan_active,"archived_at":None,
                                   "payment_url":db.plan_url})
                if sql.startswith("SELECT 1 FROM offer_acceptances a"):
                    return Result((1,) if db.offer_accepted else None)
                if sql.startswith("SELECT 1 FROM tribute_checkout_intents i"):
                    return Result((1,) if db.checkout_intent else None)
                if sql.startswith("SELECT pg_advisory_xact_lock"):
                    return Result(True)
                if sql.startswith("SELECT 1 FROM users WHERE"):
                    return Result((1,))
                if sql.startswith("SELECT telegram_id,subscription_row_id FROM tribute_auto_buyers"):
                    return Result(db.binding.copy() if db.binding else None)
                if sql.startswith("SELECT id,telegram_id,paid_until FROM subscriptions"):
                    return Result(db.sub.copy() if db.sub else None)
                if sql.startswith("SELECT 1 FROM tribute_payment_test_links"):
                    return Result((1,))
                if sql.startswith("INSERT INTO subscriptions"):
                    db.sub={"id":77,"telegram_id":db.user,"paid_until":p["expiry"]}
                    return Result(77)
                if sql.startswith("UPDATE subscriptions"):
                    assert db.sub is not None
                    db.sub["paid_until"]=max(db.sub["paid_until"],p["expiry"])
                    return Result(None)
                if sql.startswith("INSERT INTO tribute_auto_buyers"):
                    assert db.binding is None
                    db.binding={"telegram_id":p["uid"],"subscription_row_id":p["sub"]}
                    return Result(None)
                if sql.startswith("INSERT INTO verified_payment_receipts"):
                    assert p["event_key"] not in db.receipts
                    db.receipts[p["event_key"]]=p["sub"]
                    return Result(91)
                if sql.startswith("INSERT INTO tribute_invite_outbox"):
                    db.outbox.add(p["id"])
                    return Result(None)
                if sql.startswith("UPDATE payment_webhook_events SET status='processed'"):
                    db.events[p["id"]]["status"]="processed"
                    return Result(None)
                if sql.startswith("INSERT INTO subscription_audit_log"):
                    db.audit.append(p)
                    return Result(None)
                raise AssertionError("Unexpected SQL: "+sql[:120])
        return Session()


class TributeAutoTransactionTests(unittest.TestCase):
    def test_new_then_renewed_is_single_paid_subscriber_and_two_unique_events(self):
        state=MemoryDB()
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            first=asyncio.run(activate_auto_event(1))
            self.assertEqual(first,"processed")
            self.assertEqual(state.sub["id"],77)
            self.assertEqual(state.sub["telegram_id"],8609932510)
            self.assertEqual(state.binding["subscription_row_id"],77)
            self.assertEqual(state.events[1]["status"],"processed")
            self.assertEqual(state.outbox,{1})
            self.assertEqual(len(state.receipts),1)
            self.assertEqual(asyncio.run(activate_auto_event(1)),"already_handled")
            self.assertEqual(len(state.receipts),1)
            state.add_event(2,"renewed_subscription",32)
            renewed=asyncio.run(activate_auto_event(2))
            self.assertEqual(renewed,"processed")
            self.assertEqual(state.sub["id"],77)
            self.assertEqual(state.sub["paid_until"].day,
                             (state.now+timedelta(days=32)).day)
            self.assertEqual(state.outbox,{1,2})
            self.assertEqual(len(state.receipts),2)
            self.assertEqual(len(state.audit),2)
            self.assertFalse(any("access_grants" in sql or
                                 "ban_chat_member" in sql for sql in state.statements))

    def make_real_public_checkout(self):
        from db.tribute_shared_checkout import PRODUCTION_OFFER_ID, PRODUCTION_URL
        state=MemoryDB()
        state.offer.update({
            "subscription_id":PRODUCTION_OFFER_ID,
            "period_id":529034,"channel_id":547415,"amount_minor":4900,
            "currency":"eur","period":"quarterly",
            "plan_id":52,"require_test_claim":False,
        })
        state.plan_price=Decimal("49")
        state.plan_currency="EUR"
        state.plan_active=True
        state.plan_url=PRODUCTION_URL
        state.events[1]["payload"]["details"].update({
            "subscription_id":PRODUCTION_OFFER_ID,
            "period_id":529034,"channel_id":547415,
            "amount":4900,"currency":"eur","period":"quarterly",
            "expires_at":(state.now+timedelta(days=90)).isoformat(),
        })
        return state

    def test_shared_link_accepts_three_month_payment_without_any_plan_selection(self):
        state=self.make_real_public_checkout()
        with patch.dict(sys.modules, {
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            self.assertEqual(asyncio.run(activate_auto_event(1)),"processed")
        self.assertEqual(state.sub["telegram_id"],8609932510)
        self.assertEqual(state.sub["paid_until"],state.now+timedelta(days=90))
        self.assertEqual(state.outbox,{1})
        self.assertEqual(state.receipts["payment-1"],77)
        self.assertTrue(any("tribute_checkout_intents" in sql for sql in state.statements))
        self.assertFalse(any("subscription_plan_selections" in sql for sql in state.statements))

    def test_shared_link_payment_without_bot_checkout_intent_cannot_grant_access(self):
        state=self.make_real_public_checkout()
        state.checkout_intent=False
        with patch.dict(sys.modules, {
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            from db.tribute_auto import AutoPaymentError
            with self.assertRaises(AutoPaymentError):
                asyncio.run(activate_auto_event(1))
        self.assertIsNone(state.sub)
        self.assertEqual(state.outbox,set())
        self.assertFalse(state.receipts)

    def test_shared_link_paid_period_must_match_signed_price_and_id(self):
        state=self.make_real_public_checkout()
        state.events[1]["payload"]["details"]["period_id"]=529035
        with patch.dict(sys.modules, {
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            from db.tribute_auto import AutoPaymentError
            with self.assertRaises(AutoPaymentError):
                asyncio.run(activate_auto_event(1))
        self.assertIsNone(state.sub)
        self.assertFalse(state.receipts)

    def test_unsigned_event_is_rejected_before_any_write(self):
        state=MemoryDB()
        state.events[1]["signature_valid"]=False
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            from db.tribute_auto import AutoPaymentError
            with self.assertRaises(AutoPaymentError):
                asyncio.run(activate_auto_event(1))
        self.assertIsNone(state.sub)
        self.assertFalse(state.outbox)
        self.assertFalse(state.receipts)

    def test_different_telegram_id_cannot_claim_same_existing_tribute_buyer(self):
        state=MemoryDB()
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=state.session_factory)
        }):
            asyncio.run(activate_auto_event(1))
            state.add_event(2,"renewed_subscription",32)
            state.events[2]["payload"]["details"]["telegram_user_id"]=999999999
            from db.tribute_auto import AutoPaymentError
            with self.assertRaises(AutoPaymentError):
                asyncio.run(activate_auto_event(2))
        self.assertEqual(state.sub["telegram_id"],8609932510)
        self.assertEqual(len(state.receipts),1)
        self.assertEqual(state.outbox,{1})


if __name__=="__main__":
    unittest.main()
