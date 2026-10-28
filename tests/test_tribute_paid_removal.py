"""Zero-grace paid-only Tribute removal tests. No production Telegram calls."""
import asyncio
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from db.tribute_paid_removal import (
    PRODUCTION_OFFER_ID, eligible_expired_tribute_members,
    initialize_tribute_paid_removal,
    set_tribute_paid_removal_enabled, TributeRemovalError,
)
from bot.group_removal_worker import removal_eligibility


class FakeResult:
    def __init__(self,value):
        self.value=value
    def mappings(self):
        return self
    def all(self):
        return self.value
    def scalar_one_or_none(self):
        return self.value
    def scalar_one(self):
        return self.value


class FakeSession:
    queries=[]
    flags={}
    @classmethod
    def reset(cls):
        cls.queries=[]
        cls.flags={
            "paid":True, "global_removal":False,
            "group":-100111222333, "offer_count":4,
        }

    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    def begin(self):
        return self

    async def execute(self,statement,params=None):
        sql=" ".join(str(statement).split())
        self.queries.append((sql,params or {}))
        if sql.startswith("SELECT DISTINCT m.telegram_id,m.chat_id"):
            # Simulated selected record; production SQL constrains eligibility.
            return FakeResult([{"telegram_id":12345,"chat_id":-100111222333}])
        if sql.startswith("SELECT enabled FROM tribute_auto_settings"):
            return FakeResult(FakeSession.flags["paid"])
        if sql.startswith("SELECT removal_enabled"):
            return FakeResult(FakeSession.flags["global_removal"])
        if sql.startswith("SELECT chat_id FROM telegram_group_settings"):
            return FakeResult(FakeSession.flags["group"])
        if sql.startswith("SELECT COUNT(*) FROM tribute_auto_offers"):
            return FakeResult(FakeSession.flags["offer_count"])
        if sql.startswith("UPDATE tribute_paid_removal_settings"):
            return FakeResult(None)
        if sql.startswith("INSERT INTO subscription_audit_log"):
            return FakeResult(None)
        raise AssertionError("Unexpected SQL: "+sql[:100])


class FakeEngine:
    calls=[]
    def begin(self):
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    async def execute(self,statement,params=None):
        self.calls.append(" ".join(str(statement).split()))
        return FakeResult(None)


class TributePaidRemovalTests(unittest.TestCase):
    def setUp(self):
        FakeSession.reset()

    def test_schema_is_disabled_by_default(self):
        FakeEngine.calls=[]
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(engine=FakeEngine())
        }):
            asyncio.run(initialize_tribute_paid_removal())
        statements=" ".join(FakeEngine.calls)
        self.assertIn("enabled BOOLEAN NOT NULL DEFAULT FALSE",statements)
        self.assertIn("ON CONFLICT (id) DO NOTHING",statements)

    def test_exact_signed_expiry_no_grace_and_managed_only(self):
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeSession)
        }):
            rows=asyncio.run(eligible_expired_tribute_members(-100111222333))
        self.assertEqual(rows,[{"telegram_id":12345,"chat_id":-100111222333}])
        sql,params=FakeSession.queries[0]
        self.assertEqual(params["offer"],PRODUCTION_OFFER_ID)
        self.assertEqual(params["chat"],-100111222333)
        self.assertIn("m.removed_at IS NULL",sql)
        self.assertIn("JOIN tribute_auto_buyers b",sql)
        self.assertIn("JOIN subscriptions s",sql)
        self.assertIn("s.provider='tribute'",sql)
        self.assertIn("s.paid_until <= NOW()",sql)
        self.assertIn("r.enabled=TRUE",sql)
        self.assertIn("a.enabled=TRUE",sql)
        self.assertIn("general.removal_enabled=FALSE",sql)
        self.assertNotIn("INTERVAL '7 day",sql)
        self.assertNotIn("grace_hours",sql)
        self.assertNotIn("ban_chat_member",sql)

    def test_all_other_active_future_or_permanent_rights_protect_member(self):
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeSession)
        }):
            asyncio.run(eligible_expired_tribute_members(-100111222333))
        sql,_=FakeSession.queries[0]
        self.assertIn("NOT EXISTS ( SELECT 1 FROM access_grants g",sql)
        self.assertIn("g.starts_at>NOW()",sql)
        self.assertIn("g.ends_at IS NULL OR g.ends_at>NOW()",sql)
        self.assertIn("g.revoked_at IS NULL",sql)
        self.assertIn("NOT EXISTS ( SELECT 1 FROM subscriptions active_sub",sql)
        self.assertIn("active_sub.paid_until>NOW()",sql)
        self.assertIn("active_sub.paid_until IS NULL",sql)

    def test_signed_unprocessed_renewal_prevents_premature_kick(self):
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeSession)
        }):
            asyncio.run(eligible_expired_tribute_members(-100111222333))
        sql,_=FakeSession.queries[0]
        self.assertIn("payment_webhook_events e",sql)
        self.assertIn("e.signature_valid=TRUE",sql)
        self.assertIn("'pending_review','needs_attention'",sql)
        self.assertIn("'renewed_subscription'",sql)
        self.assertIn("e.payload->'details'->>'telegram_user_id'",sql)

    def test_invalid_group_never_triggers_query(self):
        self.assertEqual(asyncio.run(eligible_expired_tribute_members(0)),[])
        self.assertEqual(asyncio.run(eligible_expired_tribute_members(999)),[])

    def test_owner_must_opt_in_and_payment_processor_must_be_live(self):
        FakeSession.flags["paid"]=False
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeSession)
        }):
            with self.assertRaises(TributeRemovalError):
                asyncio.run(set_tribute_paid_removal_enabled(True,"owner"))
        self.assertFalse(any("UPDATE tribute_paid_removal_settings" in sql
                             for sql,_ in FakeSession.queries))

    def test_unrelated_global_removals_or_missing_offer_block_enable(self):
        for name,value in (
            ("global_removal",True),("group",None),("offer_count",3)
        ):
            FakeSession.reset()
            FakeSession.flags[name]=value
            with self.subTest(name=name):
                with patch.dict(sys.modules,{
                    "db.database":SimpleNamespace(SessionLocal=FakeSession)
                }):
                    with self.assertRaises(TributeRemovalError):
                        asyncio.run(set_tribute_paid_removal_enabled(True,"owner"))
                self.assertFalse(any("UPDATE tribute_paid_removal_settings" in sql
                                     for sql,_ in FakeSession.queries))

    def test_enabled_can_be_turned_off_in_emergency(self):
        FakeSession.flags["paid"]=False
        with patch.dict(sys.modules,{
            "db.database":SimpleNamespace(SessionLocal=FakeSession)
        }):
            asyncio.run(set_tribute_paid_removal_enabled(False,"owner"))
        self.assertTrue(any("UPDATE tribute_paid_removal_settings" in sql
                            for sql,_ in FakeSession.queries))

    def test_worker_rechecks_actual_access_and_paid_expiry(self):
        import bot.group_removal_worker as worker
        with patch.object(worker,"automation_settings",new_callable=AsyncMock,
                          return_value={"grace_hours":24,"removal_enabled":False}),\
             patch.object(worker,"has_group_access",new_callable=AsyncMock,
                          return_value=False),\
             patch.object(worker,"eligible_removal_tests",new_callable=AsyncMock,
                          return_value=[]),\
             patch.object(worker,"eligible_expired_tribute_members",
                          new_callable=AsyncMock,return_value=[
                              {"telegram_id":12345,"chat_id":-100111222333}
                          ]):
            self.assertTrue(asyncio.run(removal_eligibility(12345,-100111222333)))
            self.assertFalse(asyncio.run(removal_eligibility(77777,-100111222333)))

        with patch.object(worker,"automation_settings",new_callable=AsyncMock,
                          return_value={"grace_hours":0,"removal_enabled":False}),\
             patch.object(worker,"has_group_access",new_callable=AsyncMock,
                          return_value=True),\
             patch.object(worker,"eligible_removal_tests",new_callable=AsyncMock,
                          return_value=[]),\
             patch.object(worker,"eligible_expired_tribute_members",
                          new_callable=AsyncMock) as paid:
            self.assertFalse(asyncio.run(removal_eligibility(12345,-100111222333)))
            paid.assert_not_awaited()

    def test_only_owner_ui_may_enable_narrow_removal(self):
        root=Path(__file__).resolve().parents[1]
        admin=(root/"admin/main.py").read_text("utf-8")
        bot=(root/"bot/main.py").read_text("utf-8")
        worker=(root/"bot/group_removal_worker.py").read_text("utf-8")
        ui=(root/"admin/templates/tribute_auto.html").read_text("utf-8")
        self.assertIn('"/subscriptions/tribute-auto/removal",',admin)
        self.assertIn('csrf_check=Depends(verify_csrf)',admin)
        self.assertIn('user=Depends(get_current_user)',admin)
        self.assertIn('can_restrict_members',admin)
        self.assertIn('await initialize_tribute_paid_removal()',admin)
        self.assertIn('await initialize_tribute_paid_removal()',bot)
        self.assertIn('eligible_expired_tribute_members(chat_id)',worker)
        self.assertIn('action="/subscriptions/tribute-auto/removal"',ui)
        self.assertIn("0 часов отсрочки",ui)
        self.assertNotIn("await save_automation_settings(",worker)


if __name__=="__main__":
    unittest.main()
