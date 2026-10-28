"""Constrain the optional, disruptive $1 checkout Telegram API smoke test."""
import asyncio
from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from aiogram.enums import ChatMemberStatus

from scripts import smoke_test_tribute_test_member as smoke


def approved():
    return {
        "telegram_id": 1234567890,
        "trb_user_id": "T-12345",
        "subscription_row_id": 42,
        "chat_id": -100123,
        "chat_title": "TEST ONLY",
        "paid_until": datetime(2026,11,28,17,44,35,tzinfo=timezone.utc),
        "has_signed_payment": True,
        "managed_join": True,
        "verified_bot_invite": True,
        "any_manual_rights": False,
        "other_active_subscription": False,
        "has_production_purchase": False,
    }


class FakeResult:
    def __init__(self,rows):
        self.rows=rows
    def mappings(self):
        return self
    def all(self):
        return self.rows


class FakeSession:
    records=[]
    queries=[]
    def __init__(self):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self,*args):
        return False
    async def execute(self,stmt,params):
        self.queries.append((" ".join(str(stmt).split()),params))
        return FakeResult(self.records)


class SmokeTestSafety(unittest.TestCase):
    def setUp(self):
        FakeSession.records=[approved()]
        FakeSession.queries=[]

    def run_async(self,coro):
        return asyncio.run(coro)

    def test_signed_payment_and_bot_join_required(self):
        with patch.object(smoke,"SessionLocal",FakeSession):
            row=self.run_async(smoke.read_test_buyer(1234567890))
        self.assertEqual(row["telegram_id"],1234567890)
        sql,params=FakeSession.queries[-1]
        self.assertEqual(params["offer"],279302)
        self.assertIs(type(params["offer"]), int)
        self.assertEqual(params["offer_ref"], "279302")
        self.assertIs(type(params["offer_ref"]), str)
        self.assertIn("b.offer_id=:offer", sql)
        self.assertIn("CONCAT('test:', CAST(:offer_ref AS TEXT), ':', b.trb_user_id)", sql)
        self.assertNotIn("CONCAT('test:', :offer_ref, ':', b.trb_user_id)", sql)
        self.assertNotIn("CAST(:offer AS TEXT)", sql)
        self.assertEqual(params["period_text"],"528940")
        self.assertEqual(params["plan_code"],"tribute_test_279302_monthly")
        self.assertEqual(params["uid"],1234567890)
        for clause in (
            "e.signature_valid=TRUE",
            "e.status='processed'",
            "e.payload->>'name'='new_subscription'",
            "sub.paid_until>NOW()",
            "sub.provider='tribute'",
            "sub.status='active'",
            "FROM subscription_managed_members",
            "FROM telegram_group_invites",
            "FROM tribute_auto_buyers",
        ):
            self.assertIn(clause,sql)

    def test_rejects_missing_signed_webhook_join_or_extra_rights(self):
        problems=[
            ("has_signed_payment",False),
            ("managed_join",False),
            ("verified_bot_invite",False),
            ("any_manual_rights",True),
            ("other_active_subscription",True),
            ("has_production_purchase",True),
        ]
        with patch.object(smoke,"SessionLocal",FakeSession):
            for field,val in problems:
                rec=approved()
                rec[field]=val
                FakeSession.records=[rec]
                with self.subTest(field=field),self.assertRaises(smoke.UnsafeTest):
                    self.run_async(smoke.read_test_buyer(1234567890))

    def test_rejects_unknown_buyer_or_nonpositive_uid(self):
        with patch.object(smoke,"SessionLocal",FakeSession):
            FakeSession.records=[]
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.read_test_buyer(1234567890))
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.read_test_buyer(0))

    def test_double_check_uses_same_identity_before_telegram_kick(self):
        obj=approved()
        bot=SimpleNamespace(
            ban_chat_member=AsyncMock(),unban_chat_member=AsyncMock()
        )
        with patch.object(smoke,"preflight",new_callable=AsyncMock,return_value=obj) as pre,\
             patch.object(smoke,"mark_managed_removed",new_callable=AsyncMock) as mark,\
             patch.object(smoke,"send_group_invitation",new_callable=AsyncMock,
                          return_value="sent") as invite:
            result=self.run_async(smoke.smoke_kick_and_reinvite(1234567890,bot))
            self.assertEqual(result,"sent")
            self.assertEqual(pre.await_count,2)
            bot.ban_chat_member.assert_awaited_once_with(
                -100123,1234567890,revoke_messages=False
            )
            bot.unban_chat_member.assert_awaited_once_with(
                -100123,1234567890,only_if_banned=True
            )
            mark.assert_awaited_once_with(1234567890,-100123)
            invite.assert_awaited_once_with(
                bot,1234567890,actor="smoke_test_verified_tribute_279302"
            )

    def test_changed_payment_blocks_all_telegram_actions(self):
        second=approved()
        second["subscription_row_id"]=100
        bot=SimpleNamespace(
            ban_chat_member=AsyncMock(),unban_chat_member=AsyncMock()
        )
        with patch.object(smoke,"preflight",new_callable=AsyncMock,
                          side_effect=[approved(),second]),\
             patch.object(smoke,"mark_managed_removed",new_callable=AsyncMock),\
             patch.object(smoke,"send_group_invitation",new_callable=AsyncMock):
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.smoke_kick_and_reinvite(1234567890,bot))
            bot.ban_chat_member.assert_not_awaited()
            bot.unban_chat_member.assert_not_awaited()

    def test_preflight_rejects_bot_without_rights_or_member_admin(self):
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
            get_chat_member=AsyncMock(side_effect=[
                SimpleNamespace(status=ChatMemberStatus.MEMBER),
            ])
        )
        with patch.object(smoke,"read_test_buyer",new_callable=AsyncMock,
                          return_value=approved()):
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.preflight(1234567890,bot))

        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
            get_chat_member=AsyncMock(side_effect=[
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR,
                                can_restrict_members=True),
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR),
            ])
        )
        with patch.object(smoke,"read_test_buyer",new_callable=AsyncMock,
                          return_value=approved()):
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.preflight(1234567890,bot))

    def test_preflight_uses_actual_live_paid_entitlement(self):
        bot=SimpleNamespace(
            get_me=AsyncMock(return_value=SimpleNamespace(id=123)),
            get_chat_member=AsyncMock(side_effect=[
                SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR,
                                can_restrict_members=True),
                SimpleNamespace(status=ChatMemberStatus.MEMBER),
            ])
        )
        with patch.object(smoke,"read_test_buyer",new_callable=AsyncMock,
                          return_value=approved()),\
             patch("db.group_access.has_group_access",new_callable=AsyncMock,
                   return_value=False):
            with self.assertRaises(smoke.UnsafeTest):
                self.run_async(smoke.preflight(1234567890,bot))

    def test_implementation_never_modifies_subscription_rights(self):
        source=(smoke.ROOT/"scripts/smoke_test_tribute_test_member.py").read_text("utf-8")
        for forbidden in (
            "UPDATE subscriptions", "DELETE FROM subscriptions",
            "UPDATE access_grants", "DELETE FROM access_grants",
            "UPDATE payment_webhook_events",
            "DELETE FROM payment_webhook_events",
            "INSERT INTO subscriptions",
        ):
            self.assertNotIn(forbidden,source)
        self.assertIn('parser.add_argument("--execute", action="store_true")',source)
        self.assertIn("KICK_ONLY_VERIFIED_ONE_DOLLAR_TEST_ACCOUNT",source)


if __name__ == "__main__":
    unittest.main()
