"""Safely remove a managed member after an explicit administrative revoke.

Never remove legacy members unless individually opted in.
Never remove on an ordinary expiry or while a different entitlement exists.
"""
import logging
import os
from sqlalchemy import text
from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from db.database import SessionLocal
from db.group_access import group_settings, has_group_access, group_removal_test, finish_group_removal
from db.subscription_automation import mark_managed_removed

logger = logging.getLogger(__name__)


async def remove_after_revoke(telegram_id: int) -> str:
    group = await group_settings()
    chat_id = group.get("chat_id")
    if not chat_id:
        return "no_group"
    test = await group_removal_test(telegram_id)
    test_enabled = bool(test and test["enabled"] and test["chat_id"] == chat_id)
    async with SessionLocal() as session:
        managed = bool((await session.execute(text("""
            SELECT 1 FROM subscription_managed_members
            WHERE telegram_id=:uid AND chat_id=:chat AND removed_at IS NULL
        """), {"uid":telegram_id,"chat":chat_id})).first())
        future = bool((await session.execute(text("""
            SELECT 1 FROM access_grants WHERE telegram_id=:uid
              AND resource='community' AND revoked_at IS NULL AND starts_at>NOW()
        """), {"uid":telegram_id})).first())
        perpetual = bool((await session.execute(text("""
            SELECT 1 FROM subscriptions WHERE telegram_id=:uid
              AND status='active' AND paid_until IS NULL
        """), {"uid":telegram_id})).first())
    if not (test_enabled or managed):
        return "not_managed"
    if future or perpetual or await has_group_access(telegram_id):
        return "other_access"
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            member = await bot.get_chat_member(chat_id, telegram_id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                return "admin"
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (
                member.status == ChatMemberStatus.RESTRICTED and not member.is_member
            ):
                await mark_managed_removed(telegram_id, chat_id)
                if test_enabled:
                    await finish_group_removal(telegram_id, "already_out")
                return "already_out"
            # Re-read database immediately before the irreversible Telegram action.
            if await has_group_access(telegram_id):
                return "other_access"
            await bot.ban_chat_member(chat_id, telegram_id, revoke_messages=False)
            try:
                await bot.unban_chat_member(chat_id, telegram_id, only_if_banned=True)
            except Exception:
                logger.exception("Failed to unban after revoke uid=%s", telegram_id)
                await mark_managed_removed(telegram_id, chat_id)
                if test_enabled:
                    await finish_group_removal(telegram_id, "unban_failed", "Не удалось снять блокировку после отзыва")
                return "unban_failed"
            await mark_managed_removed(telegram_id, chat_id)
            if test_enabled:
                await finish_group_removal(telegram_id, "removed")
            try:
                await bot.send_message(telegram_id,
                    "Доступ в «Круг Души» отозван администратором. "
                    "Если это ошибка, обратитесь к администратору.")
            except Exception:
                logger.info("Revocation DM could not be delivered uid=%s",telegram_id)
            return "removed"
    except Exception:
        logger.exception("Telegram removal after revoke failed uid=%s", telegram_id)
        return "failed"
