"""Background sender: throttled Telegram broadcasts with opt-out button."""
import asyncio
import logging
from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.broadcast import claim_recipient, finish_recipient, mark_unfinished_as_uncertain

log = logging.getLogger(__name__)
OPT_OUT_BUTTON = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="🔕 Отписаться от рассылок", callback_data="broadcast_optout")
]])

async def broadcast_worker(bot: Bot):
    await mark_unfinished_as_uncertain()
    while True:
        item = await claim_recipient()
        if not item:
            await asyncio.sleep(3)
            continue
        campaign_id, user_id, body, opted_out, button_text, button_url = item
        markup = InlineKeyboardMarkup(inline_keyboard=([[InlineKeyboardButton(text=button_text, url=button_url)]] if button_text and button_url else []) + [[InlineKeyboardButton(text="🔕 Отписаться от рассылок", callback_data="broadcast_optout")]])
        if opted_out:
            await finish_recipient(campaign_id, user_id, "skipped", "opted_out")
            continue
        try:
            # Respect Telegram's conservative throughput: no more than ~10 messages/second.
            await bot.send_message(user_id, body, reply_markup=markup)
        except TelegramRetryAfter as exc:
            # Sending result was explicitly rejected by API with retry_after.
            await asyncio.sleep(min(exc.retry_after, 120))
            try:
                await bot.send_message(user_id, body, reply_markup=OPT_OUT_BUTTON)
            except TelegramRetryAfter:
                await finish_recipient(campaign_id, user_id, "failed", "rate_limit")
            except TelegramForbiddenError:
                await finish_recipient(campaign_id, user_id, "failed", "blocked")
            except TelegramBadRequest:
                await finish_recipient(campaign_id, user_id, "failed", "invalid_recipient")
            except Exception:
                log.exception("Broadcast second attempt failed: campaign=%s", campaign_id)
                await finish_recipient(campaign_id, user_id, "uncertain", "unexpected")
            else:
                await finish_recipient(campaign_id, user_id, "sent")
        except TelegramForbiddenError:
            await finish_recipient(campaign_id, user_id, "failed", "blocked")
        except TelegramBadRequest:
            await finish_recipient(campaign_id, user_id, "failed", "invalid_recipient")
        except Exception:
            log.exception("Broadcast failed: campaign=%s", campaign_id)
            await finish_recipient(campaign_id, user_id, "uncertain", "unexpected")
        else:
            await finish_recipient(campaign_id, user_id, "sent")
        await asyncio.sleep(0.12)
