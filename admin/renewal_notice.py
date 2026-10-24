"""Best-effort Telegram notification after a manual subscription renewal."""
import logging
import os

from aiogram import Bot
from bot.group_handlers import send_group_invitation
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


async def notify_renewal(telegram_id, new_expiry, actor):
    """Report the result; never roll back already saved access."""
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            status = await send_group_invitation(bot, telegram_id, actor)
            if status == "already_member":
                local = new_expiry.astimezone(ZoneInfo("Asia/Tbilisi"))
                try:
                    await bot.send_message(
                        telegram_id,
                        "✅ Доступ в «Круг Души» продлён до "
                        + local.strftime("%d.%m.%Y %H:%M")
                        + " (Тбилиси). Вы остаётесь участником сообщества."
                    )
                except Exception:
                    log.exception("Renewal confirmation not delivered to %s", telegram_id)
                    return "message_failed"
                return "notified"
            if status == "sent":
                try:
                    local = new_expiry.astimezone(ZoneInfo("Asia/Tbilisi"))
                    await bot.send_message(
                        telegram_id,
                        "✅ Доступ в «Круг Души» продлён до "
                        + local.strftime("%d.%m.%Y %H:%M")
                        + " (Тбилиси). Приглашение уже отправлено выше."
                    )
                except Exception:
                    log.exception("Renewal date confirmation not delivered to %s",telegram_id)
                    return "message_failed"
            return status
    except Exception:
        log.exception("Renewal Telegram action failed for %s", telegram_id)
        return "failed"
