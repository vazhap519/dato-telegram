import logging
from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from db.flow import register_user, get_flow_settings, accept_offer, authorize_video_uploader, store_uploaded_video_note

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

async def upload_command_handler(message: types.Message):
    if not message.from_user or message.chat.type != "private":
        return
    args = (message.text or "").split(maxsplit=1)
    if len(args) != 2:
        await message.answer("Откройте админ-панель и создайте одноразовый код загрузки.")
        return
    if not await authorize_video_uploader(message.from_user.id, args[1].strip()):
        await message.answer("Код неверный, просрочен или уже используется.")
        return
    await message.answer("✅ Доступ подтверждён. Теперь отправьте сюда видеокружочек через Telegram (не обычный видеофайл). Код действует 15 минут.")

async def upload_video_note_handler(message: types.Message):
    if not message.from_user or message.chat.type != "private" or not message.video_note:
        return
    if await store_uploaded_video_note(message.from_user.id, message.video_note.file_id):
        await message.answer("✅ Видеокружочек сохранён! Проверьте /start и обновите страницу админ-панели.")
    else:
        await message.answer("Для загрузки видеокружочка сначала создайте код в админ-панели и отправьте /upload КОД.")
