"""Approve only our own named invite links for the intended active subscriber."""
import logging

from aiogram import types
from db.group_access import group_settings, invite_for_request, has_group_access, mark_joined

log = logging.getLogger(__name__)
INVITE_PREFIX = "dato-access-"


async def send_group_invitation(bot, telegram_id: int, actor: str):
    """Create a one-use, short-lived direct invite and send it in private chat.

    Returns: sent / already_member / no_access / not_configured.
    Caller may handle TelegramAPIError (e.g. user blocked bot).
    """
    from datetime import datetime, timedelta, timezone
    from aiogram.enums import ChatMemberStatus
    from aiogram.exceptions import TelegramAPIError
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from db.group_access import record_invite

    if not await has_group_access(telegram_id):
        return "no_access"
    config = await group_settings()
    chat_id = config.get("chat_id")
    if not chat_id:
        return "not_configured"
    try:
        member = await bot.get_chat_member(chat_id, telegram_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                             ChatMemberStatus.CREATOR) or (
                member.status == ChatMemberStatus.RESTRICTED and member.is_member):
            return "already_member"
    except TelegramAPIError:
        # get_chat_member may fail for users who are not members.
        pass

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=20)
    invite = await bot.create_chat_invite_link(
        chat_id=chat_id,
        name=f"dato-direct-{telegram_id}",
        expire_date=expires_at,
        member_limit=1,
        creates_join_request=False
    )
    try:
        await record_invite(telegram_id, chat_id, invite.invite_link, expires_at, actor)
        await bot.send_message(
            telegram_id,
            "🔐 Для вас готово приглашение в «Круг Души». "
            "Нажмите кнопку ниже — вступление произойдёт сразу, без заявки. "
            "Ссылка рассчитана на одно вступление и действует 20 минут.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚪 Вступить в сообщество", url=invite.invite_link)
            ]])
        )
    except Exception:
        # Avoid leaving a usable unrecorded link if delivery fails.
        try:
            await bot.revoke_chat_invite_link(chat_id, invite.invite_link)
        except TelegramAPIError:
            log.warning("Unable to revoke undelivered group invite")
        raise
    return "sent"


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
    """Reveal the group ID to real or anonymous group administrators."""
    from aiogram.enums import ChatMemberStatus
    if message.chat.type not in ("group", "supergroup"):
        return
    # Telegram sends anonymous admin posts as sender_chat equal to this group.
    # In that case from_user refers to Telegram's anonymous admin service account.
    anonymous_admin = (
        message.sender_chat is not None
        and message.sender_chat.id == message.chat.id
    )
    if not anonymous_admin:
        if not message.from_user:
            return
        try:
            member = await message.bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
        except Exception:
            log.exception("Could not verify /chatid administrator")
            return
        if member.status not in (
            ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR
        ):
            return
    try:
        await message.answer(f"ID группы: {message.chat.id}")
    except Exception:
        log.exception("Unable to send group ID")


async def managed_member_join_handler(event: types.ChatMemberUpdated):
    """Observe bot-issued direct-link joins; never auto-enroll legacy members."""
    from aiogram.enums import ChatMemberStatus
    from db.subscription_automation import record_managed_join
    previous=event.old_chat_member.status
    current=event.new_chat_member.status
    if current not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        return
    if previous not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        if previous != ChatMemberStatus.RESTRICTED:
            return
        if event.old_chat_member.is_member:
            return
    group=await group_settings()
    if group.get("chat_id") != event.chat.id:
        return
    invite=event.invite_link
    if not invite or not (invite.name or "").startswith("dato-direct-"):
        return
    from db.group_access import direct_invite_owner
    record = await direct_invite_owner(event.chat.id, invite.invite_link)
    if not record:
        return
    if record["telegram_id"] != event.new_chat_member.user.id:
        await remove_invalid_direct_join(event)
        return
    if record["status"] != "issued" or not record["in_time"]:
        await remove_invalid_direct_join(event)
        return
    if not await has_group_access(event.new_chat_member.user.id):
        await remove_invalid_direct_join(event)
        return
    try:
        await record_managed_join(
            event.new_chat_member.user.id,event.chat.id,invite.invite_link
        )
    except Exception:
        log.exception("Failed to record managed membership")


async def remove_invalid_direct_join(event: types.ChatMemberUpdated):
    """Evict a join using a previously invalidated bot-issued invitation."""
    from aiogram.enums import ChatMemberStatus
    if event.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        return
    uid = event.new_chat_member.user.id
    try:
        await event.bot.ban_chat_member(event.chat.id, uid, revoke_messages=False)
        await event.bot.unban_chat_member(event.chat.id, uid, only_if_banned=True)
    except Exception:
        log.exception("Unable to remove member joined with invalid direct invitation")
