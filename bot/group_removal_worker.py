"""Single-user opt-in removal test; never mass-remove Telegram members."""
import asyncio
import logging

from aiogram.enums import ChatMemberStatus
from db.group_access import (
    eligible_removal_tests, finish_group_removal, group_settings, has_group_access
)

logger=logging.getLogger(__name__)


async def group_removal_test_worker(bot):
    while True:
        try:
            group=await group_settings()
            for target in await eligible_removal_tests():
                uid=target["telegram_id"]
                chat_id=target["chat_id"]
                if group.get("chat_id") != chat_id:
                    await finish_group_removal(uid,"error","Подключённая группа изменилась")
                    continue
                if await has_group_access(uid):
                    continue
                try:
                    member=await bot.get_chat_member(chat_id,uid)
                    if member.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.CREATOR):
                        await finish_group_removal(uid,"skipped_admin","Администраторы исключены из теста")
                        continue
                    if member.status in (ChatMemberStatus.LEFT,ChatMemberStatus.KICKED) or (
                        member.status == ChatMemberStatus.RESTRICTED and not member.is_member
                    ):
                        await finish_group_removal(uid,"already_out")
                        continue
                    # Re-check access immediately before Telegram action.
                    if await has_group_access(uid):
                        continue
                    # Remove via ban then immediately unban, so the account can join
                    # again after renewal; never leave a permanent ban.
                    await bot.ban_chat_member(chat_id,uid,revoke_messages=False)
                    try:
                        await bot.unban_chat_member(chat_id,uid,only_if_banned=True)
                    except Exception:
                        logger.exception("Unban failed after test removal, uid=%s",uid)
                        await finish_group_removal(uid,"error","Удалён, но разблокировка не удалась")
                        continue
                    await finish_group_removal(uid,"removed")
                    try:
                        await bot.send_message(uid,"⏳ Срок тестового доступа в «Круг Души» истёк, поэтому доступ к группе завершён. После продления можно получить новое приглашение через /subscription.")
                    except Exception:
                        logger.info("Unable to notify removed user %s",uid)
                except Exception as exc:
                    logger.exception("Group test removal failed uid=%s",uid)
                    await finish_group_removal(uid,"error",str(exc)[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Group removal test loop failed")
        await asyncio.sleep(30)
