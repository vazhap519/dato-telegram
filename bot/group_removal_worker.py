"""Safe, explicit subscription expiry removals.

No Telegram ban is possible until a second positive eligibility check.
Personal tests and managed members both honor the configured grace period.
"""
import asyncio
import logging

from aiogram.enums import ChatMemberStatus
from db.group_access import (
    eligible_removal_tests, finish_group_removal, group_settings,
    has_group_access, rearm_renewed_removal_tests,
)
from db.subscription_automation import (
    automation_settings, eligible_managed_removals, mark_managed_removed,
)

logger = logging.getLogger(__name__)


async def removal_eligibility(uid, chat_id):
    """Fail closed unless the subscriber explicitly meets an eligible path."""
    settings = await automation_settings()
    if await has_group_access(uid):
        return False
    tests = await eligible_removal_tests(settings["grace_hours"], limit=1000)
    if any(x["telegram_id"] == uid and x["chat_id"] == chat_id for x in tests):
        return True
    if not settings["removal_enabled"]:
        return False
    managed = await eligible_managed_removals(chat_id, settings["grace_hours"], limit=1000)
    return any(x["telegram_id"] == uid for x in managed)


async def group_removal_test_worker(bot):
    while True:
        try:
            await rearm_renewed_removal_tests()
            group = await group_settings()
            settings = await automation_settings()
            chat_id = group.get("chat_id")
            if not chat_id:
                await asyncio.sleep(30)
                continue
            test_targets = await eligible_removal_tests(settings["grace_hours"])
            targets = list(test_targets)
            if settings["removal_enabled"]:
                targets += await eligible_managed_removals(chat_id, settings["grace_hours"])
            logger.info(
                "Removal scan: group=%s grace=%sh global=%s candidates=%s",
                chat_id, settings["grace_hours"], settings["removal_enabled"],
                len(targets),
            )
            seen = set()
            for target in targets:
                uid = target["telegram_id"]
                if uid in seen or target["chat_id"] != chat_id:
                    continue
                seen.add(uid)
                try:
                    # If a renewal, grace-period change, or disabling of
                    # automation happened since selection, do NOT touch Telegram.
                    if not await removal_eligibility(uid, chat_id):
                        logger.info("Skipping removal: eligibility not confirmed uid=%s", uid)
                        continue

                    member = await bot.get_chat_member(chat_id, uid)
                    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                        await finish_group_removal(uid, "skipped_admin")
                        continue
                    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED) or (
                        member.status == ChatMemberStatus.RESTRICTED and not member.is_member
                    ):
                        await mark_managed_removed(uid, chat_id)
                        await finish_group_removal(uid, "already_out")
                        continue

                    # Mandatory *positive* revalidation directly before ban.
                    if not await removal_eligibility(uid, chat_id):
                        logger.info("Removal prevented by second check uid=%s", uid)
                        continue

                    logger.info("Removing eligible subscriber uid=%s chat=%s", uid, chat_id)
                    await bot.ban_chat_member(chat_id, uid, revoke_messages=False)
                    try:
                        await bot.unban_chat_member(chat_id, uid, only_if_banned=True)
                    except Exception:
                        logger.exception("Unban failed after group removal uid=%s", uid)
                        await mark_managed_removed(uid, chat_id)
                        await finish_group_removal(
                            uid, "unban_failed",
                            "Удалён, но разблокировка не удалась. Требуется проверка группы."
                        )
                        continue
                    await mark_managed_removed(uid, chat_id)
                    await finish_group_removal(uid, "removed")
                    try:
                        await bot.send_message(
                            uid,
                            "⏳ Срок доступа в «Круг Души» и льготный период истекли. "
                            "После продления получите новое приглашение через /subscription."
                        )
                    except Exception:
                        logger.info("Expiry notice not delivered uid=%s", uid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Removal failed for uid=%s", uid)
                    await finish_group_removal(uid, "error", str(exc)[:300])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Subscription removal cycle failed")
        await asyncio.sleep(30)
