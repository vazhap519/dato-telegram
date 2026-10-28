import logging
from zoneinfo import ZoneInfo

TBILISI_TZ = ZoneInfo('Asia/Tbilisi')
from aiogram import types, F
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from db.flow import register_user, get_flow_settings, accept_offer, authorize_video_uploader, store_uploaded_video_note, record_subscription_click, has_accepted_offer
from db.subscriptions import available_payment_plans, select_payment_plan, subscription_snapshot
from db.group_access import group_settings
from db.tribute_payment_test import claim_tribute_test_link, TEST_PAYMENT_URL, TEST_BROWSER_URL, TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID
from db.tribute_auto import is_auto_processing_enabled
from db.tribute_shared_checkout import begin_shared_checkout
from bot.group_handlers import send_group_invitation

logger = logging.getLogger(__name__)

def offer_buttons(offer_url):
    rows = []
    if offer_url and offer_url.startswith(("https://", "http://")):
        rows.append([InlineKeyboardButton(text="📄 Ознакомиться с офертой", url=offer_url)])
    rows.append([InlineKeyboardButton(text="✅ Принимаю условия", callback_data="accept_offer")])
    rows.append([InlineKeyboardButton(text="🛟 Помощь", callback_data="help_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def subscription_intro(settings):
    """Single reusable subscription step for accepted users."""
    message_text = (settings.get("subscription_text") or "").strip()
    if not message_text:
        message_text = "Вы приняли условия. Нажмите «Оформить подписку», чтобы выбрать срок участия в Tribute."
    return message_text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="subscription_click")],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="🛟 Помощь", callback_data="help_menu")]
    ])



def account_keyboard(can_subscribe=True, can_invite=False):
    rows = []
    if can_subscribe:
        rows.append([InlineKeyboardButton(
            text="💳 Оформить подписку", callback_data="subscription_click"
        )])
    if can_invite:
        rows.append([InlineKeyboardButton(
            text="🚪 Получить приглашение", callback_data="request_group_invite"
        )])
    rows.append([InlineKeyboardButton(
        text="🔄 Обновить статус", callback_data="my_subscription"
    )])
    rows.append([InlineKeyboardButton(text="🛟 Помощь",callback_data="help_menu")])
    rows.append([InlineKeyboardButton(
        text="⬅️ Назад", callback_data="back_to_subscription"
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_subscription_account(bot, telegram_id, message=None):
    snapshot = await subscription_snapshot(telegram_id)
    current = snapshot["grant"]
    paid = snapshot["subscription"]
    chosen = snapshot["selection"]
    lines = ["👤 Моя подписка — «Круг Души»", ""]
    if current:
        if current["grant_type"] == "lifetime" and current["ends_at"] is None:
            lines.append("✅ Доступ: постоянный")
        elif current["ends_at"]:
            lines.append("✅ Внутренний доступ действителен до: " +
                         current["ends_at"].astimezone(TBILISI_TZ).strftime("%d.%m.%Y"))
        else:
            lines.append("✅ Доступ назначен администратором")
        lines.append("ℹ️ Если вы ещё не в группе, получите приглашение кнопкой ниже.")
    elif paid:
        lines.append("✅ Оплаченный период учтён до: " +
                     paid["paid_until"].astimezone(TBILISI_TZ).strftime("%d.%m.%Y"))
        if paid["plan_name"]:
            lines.append("Тариф: " + paid["plan_name"])
    else:
        lines.append("ℹ️ Подтверждённой активной подписки в нашей системе пока нет.")
    if chosen:
        lines.extend(["", "Последний выбранный тариф: " + chosen["plan_name"],
                      "Выбор тарифа не является подтверждением оплаты."])
    if await is_auto_processing_enabled():
        lines.extend(["", "Оплата Tribute проверяется автоматически для подключённых тарифов."])
    else:
        lines.extend(["", "Автоматическая проверка Tribute пока выключена."])
    output = "\n".join(lines)
    group = await group_settings()
    buttons = account_keyboard(can_invite=bool(group.get("chat_id") and (current or paid)))
    if message:
        try:
            await message.edit_text(output, reply_markup=buttons)
            return
        except TelegramBadRequest:
            pass
    await bot.send_message(telegram_id, output, reply_markup=buttons)


async def request_group_invite_handler(query: types.CallbackQuery):
    if not query.from_user:
        return
    await query.answer()
    try:
        status = await send_group_invitation(query.bot, query.from_user.id, "self_service")
    except Exception:
        logger.exception("Self-service invitation failed for %s", query.from_user.id)
        await query.bot.send_message(
            query.from_user.id,
            "Не удалось отправить приглашение. Попробуйте ещё раз позже или обратитесь к администратору."
        )
        return
    responses = {
        "sent": "✅ Отправил персональное приглашение следующим сообщением.",
        "already_member": "✅ Вы уже состоите в закрытой группе.",
        "no_access": "У вас пока нет действующего доступа. Проверьте статус подписки.",
        "not_configured": "Группа пока не подключена. Обратитесь к администратору.",
    }
    await query.bot.send_message(query.from_user.id, responses.get(status, "Не удалось получить приглашение."))


async def my_subscription_command(message: types.Message):
    if not message.from_user or message.chat.type != "private":
        return
    await show_subscription_account(message.bot, message.from_user.id)


async def my_subscription_callback(query: types.CallbackQuery):
    if not query.from_user:
        return
    await query.answer()
    await show_subscription_account(query.bot, query.from_user.id, query.message)


async def handle_tribute_test_start(message: types.Message, token: str, settings: dict):
    """One-time private checkout preview, not a real subscription entitlement."""
    if message.chat.type != "private":
        return
    version = (settings.get("offer_version") or "1").strip()
    if not await has_accepted_offer(message.from_user.id, version):
        await message.answer(
            "🧪 Для тестовой оплаты сначала ознакомьтесь с условиями и примите оферту. "
            "После этого повторно откройте одноразовую ссылку из админки.",
            reply_markup=offer_buttons((settings.get("offer_url") or "").strip()),
        )
        return
    if not await claim_tribute_test_link(token, message.from_user.id):
        await message.answer(
            "Тестовая ссылка истекла или уже использована. "
            "Создайте новую в административной панели подписок."
        )
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Открыть оплату Tribute", url=TEST_PAYMENT_URL)],
        [InlineKeyboardButton(text="🌐 Оплата через браузер", url=TEST_BROWSER_URL)],
    ])
    await message.answer(
        "🧪 Проверка оплаты через нашего бота\n\n"
        "Тестовый тариф Tribute: 1 USD в месяц. "
        "На экране Tribute возможна дополнительная комиссия (ранее итог составлял 1,12 USD).\n"
        f"Предложение: {TEST_SUBSCRIPTION_ID}, период: {TEST_PERIOD_ID}.\n\n"
        "⚠️ Это настоящее списание с автоматическим продлением, пока его не отменить. "
        "Проверка нужна для изучения платёжного вебхука. "
        "После оплаты доступ в «Круг Души» автоматически НЕ предоставляется.\n\n"
        "Если согласны провести платёж, воспользуйтесь кнопкой. "
        "Затем проверьте журнал Tribute в админке.",
        reply_markup=keyboard,
    )


async def start_handler(message: types.Message):
    if not message.from_user:
        return
    is_new = await register_user(message.from_user.id, username=message.from_user.username, first_name=message.from_user.first_name, last_name=message.from_user.last_name)
    settings = await get_flow_settings()
    if not settings:
        await message.answer("Настройки бота ещё не готовы.")
        return
    # Test checkout is deliberately absent from the public plan list.
    # Only a short-lived, single-use link created by an authenticated admin works.
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("tt_"):
        await handle_tribute_test_start(message, parts[1].strip(), settings)
        return
    version = (settings.get("offer_version") or "1").strip()
    if await has_accepted_offer(message.from_user.id, version):
        subscription_text, buttons = subscription_intro(settings)
        await message.answer(subscription_text, reply_markup=buttons)
        logger.info("Returning user %s skipped accepted offer %s", message.from_user.id, version)
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
    await register_user(query.from_user.id, username=query.from_user.username, first_name=query.from_user.first_name, last_name=query.from_user.last_name)
    await accept_offer(query.from_user.id, version)
    await query.answer("Условия приняты")
    if query.message:
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
    subscription_text, buttons = subscription_intro(settings)
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

def plan_button_label(plan):
    price = f"{plan['price_amount']:.2f}".rstrip("0").rstrip(".")
    term = "навсегда" if plan["is_lifetime"] else f"{plan['duration_days']} дней"
    return f"{plan['name'][:30]} · {price} {plan['currency']} · {term}"[:64]


async def subscription_click_handler(query: types.CallbackQuery):
    """One bot button, one Tribute page, four selectable paid periods.

    The click proves intent to visit an OFFER only, not an assumed payment
    period. Real term/price and entitlement are taken solely from a signed
    Tribute new_subscription webhook.
    """
    if not query.from_user:
        return
    if not await record_subscription_click(query.from_user.id):
        await query.answer("Сначала примите актуальную публичную оферту через /start.", show_alert=True)
        return
    url = await begin_shared_checkout(query.from_user.id)
    if not url:
        await query.answer(
            "Оформление подписки сейчас настраивается. Попробуйте позже.",
            show_alert=True,
        )
        return
    await query.answer()
    keyboard=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить через Tribute", url=url)],
        [InlineKeyboardButton(text="👤 Моя подписка", callback_data="my_subscription")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_subscription")],
    ])
    message_text=(
        "💫 «Круг Души» — оформление подписки\n\n"
        "Выберите срок уже на странице Tribute:\n"
        "• 1 месяц — 19 €\n"
        "• 3 месяца — 49 €\n"
        "• 6 месяцев — 95 €\n"
        "• 1 год — 179 €\n\n"
        "Tribute может добавить сервисный сбор, итог виден до оплаты. "
        "Подписка продлевается автоматически, пока вы её не отмените.\n\n"
        "После подтверждения платежа наш бот учтёт фактически оплаченный "
        "период и отправит персональное приглашение в «Круг Души». "
        "Открытие страницы оплаты само по себе доступ не предоставляет."
    )
    if query.message:
        try:
            await query.message.edit_text(message_text,reply_markup=keyboard)
            return
        except TelegramBadRequest:
            pass
    await query.bot.send_message(
        query.from_user.id,message_text,reply_markup=keyboard
    )


async def back_to_subscription_handler(query: types.CallbackQuery):
    """Return from the plan list to the subscription entry step."""
    if not query.from_user:
        return
    settings = await get_flow_settings()
    if not settings:
        await query.answer("Настройки ещё не готовы.", show_alert=True)
        return
    version = (settings.get("offer_version") or "1").strip()
    if not await has_accepted_offer(query.from_user.id, version):
        await query.answer("Пожалуйста, примите актуальную оферту через /start.", show_alert=True)
        return
    subscription_text, buttons = subscription_intro(settings)
    await query.answer()
    if query.message:
        try:
            await query.message.edit_text(subscription_text, reply_markup=buttons)
            return
        except TelegramBadRequest:
            pass
    await query.bot.send_message(query.from_user.id, subscription_text, reply_markup=buttons)


async def choose_plan_handler(query: types.CallbackQuery):
    """Old inline tariff buttons must not open legacy or private-group charges."""
    if query.from_user:
        await query.answer(
            "Срок подписки теперь выбирается непосредственно в Tribute. "
            "Откройте /start и нажмите «Оформить подписку».",
            show_alert=True,
        )


from db.broadcast import opt_out, opt_in

async def broadcast_optout_handler(query: types.CallbackQuery):
    await opt_out(query.from_user.id)
    await query.answer('Рассылки отключены')
    if query.message:
        await query.message.answer('Вы отписались от информационных рассылок. Чтобы снова получать сообщения, отправьте /notifications.')

async def broadcast_optin_handler(message: types.Message):
    if message.from_user and message.chat.type == 'private':
        await opt_in(message.from_user.id)
        await message.answer('✅ Информационные рассылки включены. Чтобы отключить необязательные сообщения, отправьте /stopnotifications.')

async def broadcast_stop_handler(message: types.Message):
    if message.from_user and message.chat.type == 'private':
        await opt_out(message.from_user.id)
        await message.answer('Необязательные рассылки и напоминания отключены. Для включения отправьте /notifications.')
