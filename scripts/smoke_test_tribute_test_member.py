#!/usr/bin/env python3
"""Deliberately targeted Telegram kick/reinvite smoke test for the REAL $1 buyer.

Default is READ-ONLY and displays a preflight report. This is NOT a simulated
payment expiration and does not modify any subscription/grant/payment record.
The destructive action is only allowed for an actually paid, bot-invited,
currently active purchaser of old one-off Tribute offer 279302.
Never use this script for a production 279348 customer.
"""
from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from dotenv import load_dotenv
from sqlalchemy import text

from db.database import SessionLocal
from db.tribute_payment_test import TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID
from db.tribute_test_activation import TEST_PLAN_CODE
from db.subscription_automation import mark_managed_removed
from bot.group_handlers import send_group_invitation

load_dotenv(ROOT / ".env")

CONFIRM = "KICK_ONLY_VERIFIED_ONE_DOLLAR_TEST_ACCOUNT"


class UnsafeTest(ValueError):
    pass


async def read_test_buyer(telegram_id: int) -> dict:
    if type(telegram_id) is not int or telegram_id <= 0:
        raise UnsafeTest("Неверный Telegram ID")
    async with SessionLocal() as s:
        rows = (await s.execute(text("""
            SELECT
                b.telegram_id, b.trb_user_id, sub.id AS subscription_row_id,
                sub.paid_until, grp.chat_id, grp.chat_title,
                EXISTS (
                    SELECT 1 FROM payment_webhook_events e
                    WHERE e.provider='tribute' AND e.signature_valid=TRUE
                      AND e.status='processed'
                      AND e.payload->>'name'='new_subscription'
                      AND e.payload->'details'->>'subscription_id'=:offer_text
                      AND e.payload->'details'->>'period_id'=:period_text
                      AND e.payload->'details'->>'telegram_user_id'=:uid_text
                      AND e.payload->'details'->>'trb_user_id'=b.trb_user_id
                      AND e.payload->'details'->>'amount'='100'
                      AND e.payload->'details'->>'currency'='usd'
                ) AS has_signed_payment,
                EXISTS (
                    SELECT 1 FROM subscription_managed_members m
                    WHERE m.telegram_id=b.telegram_id AND m.chat_id=grp.chat_id
                      AND m.removed_at IS NULL
                ) AS managed_join,
                EXISTS (
                    SELECT 1 FROM telegram_group_invites i
                    WHERE i.telegram_id=b.telegram_id AND i.chat_id=grp.chat_id
                      AND i.status='joined'
                ) AS verified_bot_invite,
                EXISTS (
                    SELECT 1 FROM access_grants g
                    WHERE g.telegram_id=b.telegram_id
                      AND g.resource='community' AND g.revoked_at IS NULL
                ) AS any_manual_rights,
                EXISTS (
                    SELECT 1 FROM subscriptions other
                    WHERE other.telegram_id=b.telegram_id AND other.id<>sub.id
                      AND other.status='active'
                      AND (other.paid_until IS NULL OR other.paid_until>NOW())
                ) AS other_active_subscription,
                EXISTS (
                    SELECT 1 FROM tribute_auto_buyers prod
                    WHERE prod.telegram_id=b.telegram_id
                ) AS has_production_purchase
            FROM tribute_test_buyer_bindings b
            JOIN subscriptions sub
              ON sub.id=b.subscription_row_id
             AND sub.telegram_id=b.telegram_id
             AND sub.provider='tribute'
             AND sub.provider_subscription_id=CONCAT('test:', CAST(:offer AS TEXT), ':', b.trb_user_id)
             AND sub.status='active'
             AND sub.paid_until>NOW()
            JOIN subscription_plans plan
              ON plan.id=sub.plan_id
             AND plan.code=:plan_code
             AND plan.is_lifetime=FALSE
            JOIN telegram_group_settings grp
              ON grp.id=1 AND grp.chat_id<0
            WHERE b.offer_id=:offer AND b.telegram_id=:uid
            LIMIT 2
        """), {
            "offer": TEST_SUBSCRIPTION_ID,
            "period_text": str(TEST_PERIOD_ID),
            "offer_text": str(TEST_SUBSCRIPTION_ID),
            "plan_code": TEST_PLAN_CODE,
            "uid": telegram_id,
            "uid_text": str(telegram_id),
        })).mappings().all()
    if len(rows) != 1:
        raise UnsafeTest(
            "Не найдена ровно одна действующая $1-подписка Tribute с проверенной привязкой покупателя"
        )
    row = dict(rows[0])
    safety = {
        "подписанный обработанный $1 платёж": row["has_signed_payment"],
        "вступление зарегистрировано нашим ботом": row["managed_join"],
        "личная ссылка нашего бота была использована": row["verified_bot_invite"],
        "нет других ручных/бессрочных прав": not row["any_manual_rights"],
        "нет другой действующей подписки": not row["other_active_subscription"],
        "нет реальной покупки нового предложения": not row["has_production_purchase"],
    }
    problems = [key for key, ok in safety.items() if ok is not True]
    if problems:
        raise UnsafeTest("Тест запрещён: " + "; ".join(problems))
    return row


async def preflight(telegram_id: int, bot: Bot) -> dict:
    row = await read_test_buyer(telegram_id)
    group_id = row["chat_id"]
    me = await bot.get_me()
    admin = await bot.get_chat_member(group_id, me.id)
    can_remove = (
        admin.status == ChatMemberStatus.CREATOR
        or (
            admin.status == ChatMemberStatus.ADMINISTRATOR
            and bool(getattr(admin, "can_restrict_members", False))
        )
    )
    if not can_remove:
        raise UnsafeTest("У нашего бота нет права исключать участников из группы")

    member = await bot.get_chat_member(group_id, telegram_id)
    if member.status != ChatMemberStatus.MEMBER:
        raise UnsafeTest(
            "Тестовый аккаунт должен быть обычным участником группы; "
            "фактический статус: " + str(member.status)
        )

    # Payment-backed entitlement must still exist immediately before kicking.
    from db.group_access import has_group_access
    if not await has_group_access(telegram_id):
        raise UnsafeTest("Действующая оплата не даёт доступ: тест отменён")

    return row


async def smoke_kick_and_reinvite(telegram_id: int, bot: Bot) -> str:
    """Perform real Telegram operations only after TWO full safety checks."""
    first = await preflight(telegram_id, bot)
    second = await preflight(telegram_id, bot)
    if (
        first["chat_id"] != second["chat_id"]
        or first["subscription_row_id"] != second["subscription_row_id"]
        or first["paid_until"] != second["paid_until"]
    ):
        raise UnsafeTest("Условия изменились между проверками: действие отменено")
    chat_id = second["chat_id"]

    # Same Telegram remove/unban API calls as the production expiry worker.
    await bot.ban_chat_member(chat_id, telegram_id, revoke_messages=False)
    # Revoke the ban immediately, so the subscriber CAN re-enter on invitation.
    await bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)

    # Bookkeeping concerns only current membership; paid_until stays untouched.
    await mark_managed_removed(telegram_id, chat_id)
    result = await send_group_invitation(
        bot, telegram_id, actor="smoke_test_verified_tribute_279302"
    )
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only $1 test buyer preflight by default; optional explicit Telegram removal + reinvite"
    )
    parser.add_argument("--telegram-id", required=True, type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.execute and args.confirm != CONFIRM:
        parser.error(
            "Запрещён вызов без --confirm " + CONFIRM
        )
    if not args.execute and args.confirm:
        parser.error("Подтверждение допускается только вместе с --execute")

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise UnsafeTest("BOT_TOKEN не найден в действующем окружении")
    async with Bot(token=token) as bot:
        row = await preflight(args.telegram_id, bot)
        print("PREFLIGHT PASS: verified REAL $1 test buyer; no other rights")
        print("Telegram ID:", args.telegram_id)
        print("Tribute offer:", TEST_SUBSCRIPTION_ID)
        print("Paid through:", row["paid_until"].isoformat())
        print("Group:", row["chat_title"], "(", row["chat_id"], ")")
        print("Bot admin can remove members: YES")
        print("Bot-managed joined membership: YES")
        if not args.execute:
            print("READ ONLY: No Telegram removals, invitations or DB writes.")
            print("For the deliberate one-account API smoke test, run with:")
            print(
                ".venv/bin/python scripts/smoke_test_tribute_test_member.py "
                "--telegram-id " + str(args.telegram_id)
                + " --execute --confirm " + CONFIRM
            )
            return 0

        print("EXECUTING: bot removes ONLY this previously verified $1 test account")
        result = await smoke_kick_and_reinvite(args.telegram_id, bot)
        print("TELEGRAM REMOVAL + UNBAN: COMPLETED")
        print("Reinvitation outcome:", result)
        print("Payment records and paid_until: UNCHANGED")
        if result != "sent":
            print(
                "INVITATION NOT SENT: check subscriber group status and request "
                "a new invitation in @krugdushi_bot using /subscription"
            )
            return 2
        print(
            "Ask the test account to open the bot's new one-use group invitation "
            "and rejoin. This validates Telegram API kick/reinvite, NOT a live "
            "production subscription expiration."
        )
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except UnsafeTest as exc:
        print("SAFETY CHECK FAILED:", str(exc), file=sys.stderr)
        raise SystemExit(1)
