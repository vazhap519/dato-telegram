import logging
from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from db.flow import register_user, get_flow_settings, accept_offer

logger = logging.getLogger(__name__)

def offer_buttons(offer_url):
    rows = []
    if offer_url and offer_url.startswith(("https://", "http://")):
        rows.append([InlineKeyboardButton(text="📄 Ознакомиться с офертой", url=offer_url)])
    rows.append([InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_offer")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def start_handler(message: types.Message):
    if not message.from_user:
        return
    is_new = await register_user(message.from_user.id)
    settings = await get_flow_settings()
    if not settings:
        await message.answer("Настройки бота ещё не готовы.")
        return
    video_id = (settings.get("video_note_file_id") or "").strip()
    if video_id:
        try:
            await message.answer_video_note(video_note=video_id)
        except Exception:
            logger.exception("Failed to deliver video note")
    offer_text = (settings.get("offer_text") or "").strip()
    if not offer_text:
        offer_text = "Перед оформлением подписки ознакомьтесь с публичной офертой и подтвердите согласие с её условиями."
    await message.answer(
        offer_text,
        reply_markup=offer_buttons((settings.get("offer_url") or "").strip())
    )
    logger.info("Onboarding started for user %s (new=%s)", message.from_user.id, is_new)

async def accept_offer_handler(query: types.CallbackQuery):
    if not query.from_user:
        return
    settings = await get_flow_settings()
    if not settings:
        await query.answer("Настройки ещё не готовы.", show_alert=True)
        return
    version = (settings.get("offer_version") or "1").strip()
    await register_user(query.from_user.id)
    await accept_offer(query.from_user.id, version)
    await query.answer("Условия приняты")
    if query.message:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    subscription_text = (settings.get("subscription_text") or "").strip()
    if not subscription_text:
        subscription_text = "Спасибо! Чтобы оформить подписку, нажмите кнопку ниже."
    subscription_url = (settings.get("subscription_url") or "").strip()
    buttons = None
    if subscription_url.startswith(("https://", "http://")):
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оформить подписку", url=subscription_url)]
        ])
    await query.bot.send_message(query.from_user.id, subscription_text, reply_markup=buttons)
