"""Approve only our own named invite links for the intended active subscriber."""
import logging

from aiogram import types
from db.group_access import group_settings, invite_for_request, has_group_access, mark_joined

log = logging.getLogger(__name__)
INVITE_PREFIX = "dato-access-"

async def group_join_request_handler(request: types.ChatJoinRequest):
    cfg = await group_settings()
    if cfg.get("chat_id") != request.chat.id:
        return
    link = request.invite_link
    if not link or not (link.name or "").startswith(INVITE_PREFIX):
        # Leave unrelated manual/Tribute requests alone.
        return
    uid = request.from_user.id
    valid = await invite_for_request(uid, request.chat.id, link.invite_link)
    allowed = await has_group_access(uid) if valid else False
    if not (valid and allowed):
        try:
            await request.bot.decline_chat_join_request(request.chat.id, uid)
        except Exception:
            log.exception("Failed to reject invalid test invite request in chat %s", request.chat.id)
        return
    try:
        await request.bot.approve_chat_join_request(request.chat.id, uid)
        await mark_joined(uid, request.chat.id, link.invite_link)
    except Exception:
        log.exception("Could not approve group join request chat=%s user=%s", request.chat.id, uid)


async def group_id_handler(message: types.Message):
    """Group administrators can discover the chat ID without external bots."""
    from aiogram.enums import ChatMemberStatus
    if not message.from_user or message.chat.type not in ("group", "supergroup"):
        return
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            return
        await message.answer(f"ID группы: `{message.chat.id}`")
    except Exception:
        log.exception("Unable to show group ID")
