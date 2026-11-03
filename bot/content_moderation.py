"""Opt-in, administrator-safe anti-spam moderation for one Telegram group."""
import hashlib
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.types import ChatPermissions
from db.group_access import group_settings
from db.group_moderation import (
    get_moderation_settings, log_violation, count_moderation_action,
)

log = logging.getLogger(__name__)

# Counters are intentionally short-lived and reset after a bot restart.
recent = defaultdict(deque)
MAX_KEYS = 3000
URL = re.compile(r"(?i)(?:https?://|www\\.|(?:[a-z0-9-]+\\.)+(?:com|net|org|ru|ge|me|io|co|info|app|xyz|site|link|online|shop|gg|tv|dev)\\b)")
INVITES = re.compile(r"(?i)(?:t\\.me/(?:joinchat/|\\+)|telegram\\.me/(?:joinchat/|\\+)|discord\\.gg/)")
REASONS = {
    "invite":"Приглашение в стороннюю группу",
    "link":"Ссылка",
    "forward":"Пересланное сообщение",
    "flood":"Слишком частые сообщения",
    "duplicate":"Повторяющееся сообщение",
}


def classify(message, cfg):
    content = (message.text or message.caption or "")
    entities = list(message.entities or []) + list(message.caption_entities or [])
    entity_urls = [
        str(getattr(entity,"url", "") or "") for entity in entities
        if getattr(entity, "type", None) in ("text_link", "url")
        or str(getattr(entity,"type","")) in ("MessageEntityType.TEXT_LINK","MessageEntityType.URL")
    ]
    # Entity URLs may be represented in the message's text instead of .url.
    has_link = bool(URL.search(content) or any(entity_urls))
    has_invite = bool(INVITES.search(content) or any(INVITES.search(url) for url in entity_urls))
    if cfg["block_invites"] and has_invite:
        return "invite"
    if cfg["block_links"] and has_link:
        return "link"
    if cfg["block_forwards"] and message.forward_origin is not None:
        return "forward"

    if not (cfg["block_flood"] or cfg["block_duplicates"]):
        return None

    uid=message.from_user.id
    key=(message.chat.id,uid)
    now=time.monotonic()
    # Keep enough history for the longest configured window; avoid unlimited growth.
    window=max(cfg["flood_window_seconds"],cfg["duplicate_window_seconds"])
    if len(recent)>MAX_KEYS:
        for stale in [k for k,v in recent.items() if not v or now-v[-1][0]>window]:
            recent.pop(stale,None)
        if len(recent)>MAX_KEYS:
            recent.clear()
    history=recent[key]
    while history and now-history[0][0]>window:
        history.popleft()
    normalized=" ".join(content.casefold().split())[:1000]
    digest=hashlib.sha256(normalized.encode()).hexdigest() if normalized else None
    flood_count=sum(now-t<=cfg["flood_window_seconds"] for t,_ in history)+1
    duplicate_count=(sum(now-t<=cfg["duplicate_window_seconds"] and h==digest
                         for t,h in history)+1) if digest and len(normalized)>=8 else 0
    history.append((now,digest))
    if cfg["block_flood"] and flood_count>cfg["flood_limit"]:
        return "flood"
    if cfg["block_duplicates"] and duplicate_count>=3:
        return "duplicate"
    return None


async def moderate_group_message(message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    # Prevent a malformed or new Telegram service field being treated as user content.
    if not message.from_user or message.from_user.is_bot or message.sender_chat:
        return
    if message.new_chat_members or message.left_chat_member or message.pinned_message:
        return
    if not (message.text or message.caption or message.forward_origin or message.photo
            or message.video or message.document or message.sticker):
        return
    try:
        group=await group_settings()
        if group.get("chat_id")!=message.chat.id:
            return
        cfg=await get_moderation_settings()
        if not cfg["content_enabled"]:
            return
        try:
            member=await message.bot.get_chat_member(message.chat.id,message.from_user.id)
        except Exception:
            log.exception("Cannot determine moderation exemption for uid=%s",message.from_user.id)
            return  # fail open: do not delete if admin status is unknown
        if member.status in (ChatMemberStatus.ADMINISTRATOR,ChatMemberStatus.CREATOR):
            return
        reason=classify(message,cfg)
        if not reason:
            return
        try:
            await message.bot.delete_message(message.chat.id,message.message_id)
        except Exception:
            log.exception("Failed to delete violating message %s",message.message_id)
            await log_violation(message.chat.id,message.from_user.id,
                                message.message_id,reason,"failed")
            await count_moderation_action(False)
            return
        await count_moderation_action(True)
        count=await log_violation(message.chat.id,message.from_user.id,
                                  message.message_id,reason,"deleted")
        if cfg["mute_minutes"]>0 and count>=cfg["warning_limit"]:
            try:
                await message.bot.restrict_chat_member(
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc)+timedelta(minutes=cfg["mute_minutes"]),
                )
                await log_violation(message.chat.id,message.from_user.id,
                                    message.message_id,reason,"muted")
            except Exception:
                log.exception("Could not restrict uid=%s",message.from_user.id)
        if cfg["notify_user"]:
            try:
                await message.bot.send_message(
                    message.from_user.id,
                    "⚠️ Сообщение в «Круге Души» удалено. Причина: "
                    + REASONS[reason] + ". Нарушений за последние 24 часа: "
                    + str(count) + ". Если это ошибка, напишите в /help."
                )
            except Exception:
                # A group member may not have opened private chat with the bot.
                log.info("Moderation notice unavailable for uid=%s",message.from_user.id)
    except Exception:
        log.exception("Content moderation processing error chat=%s",message.chat.id)
