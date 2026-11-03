"""Delete only actual Telegram group service messages in the configured group."""
import logging

from aiogram import types
from aiogram.enums import ChatType
from db.group_access import group_settings
from db.group_moderation import get_moderation_settings, count_moderation_action

logger = logging.getLogger(__name__)


def service_kind(message: types.Message):
    # Each of these is an actual Telegram Message service field.
    if message.new_chat_members:
        return "join_enabled"
    if message.left_chat_member:
        return "leave_enabled"
    if message.pinned_message:
        return "pin_enabled"
    if (
        message.new_chat_title is not None
        or message.new_chat_photo is not None
        or message.delete_chat_photo
        or message.group_chat_created
        or message.supergroup_chat_created
        or message.message_auto_delete_timer_changed is not None
    ):
        return "other_enabled"
    return None


async def delete_service_message(message: types.Message):
    """Observe service messages only. Never delete normal content or admin posts."""
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    kind = service_kind(message)
    if not kind:
        return
    try:
        group = await group_settings()
        if not group.get("chat_id") or group["chat_id"] != message.chat.id:
            return
        config = await get_moderation_settings()
        if not config.get(kind, False):
            return
        await message.bot.delete_message(message.chat.id, message.message_id)
        await count_moderation_action(True)
    except Exception:
        logger.exception("Group service message cleanup failed group=%s message=%s type=%s",
                         message.chat.id, message.message_id, kind)
        try:
            await count_moderation_action(False)
        except Exception:
            logger.exception("Could not count moderation failure")
