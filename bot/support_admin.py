"""Telegram-native support console for linked staff accounts."""
import asyncio
import logging

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp

from db.support import (list_tickets, ticket_detail, admin_reply,
                        mark_delivery, set_ticket_status, STATUSES)
from db.support_telegram import (generate_pair_code, telegram_staff, set_reply_target,
                                 pending_notifications, mark_notification)

log = logging.getLogger(__name__)

SUPPORT_APP_URL = "https://bot.arutyun.me/tg-support"

def app_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛠 Открыть админ-панель",
                             web_app=WebAppInfo(url=SUPPORT_APP_URL))
    ]])


async def install_staff_menu(bot, chat_id):
    await bot.set_chat_menu_button(
        chat_id=chat_id,
        menu_button=MenuButtonWebApp(
            text="Админ-панель", web_app=WebAppInfo(url=SUPPORT_APP_URL)
        )
    )



def keyboard(ticket_id=None,closed=False):
    if ticket_id is None:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 Обращения", callback_data="staff:list")],
        ])
    buttons=[]
    if not closed:
        buttons.append([InlineKeyboardButton(text="✍️ Ответить",callback_data=f"staff:reply:{ticket_id}")])
        buttons.append([
            InlineKeyboardButton(text="⏳ Отложить",callback_data=f"staff:status:{ticket_id}:pending"),
            InlineKeyboardButton(text="✅ Закрыть",callback_data=f"staff:status:{ticket_id}:closed"),
        ])
    else:
        buttons.append([InlineKeyboardButton(text="↩️ Открыть",callback_data=f"staff:status:{ticket_id}:open")])
    buttons.append([InlineKeyboardButton(text="🛠 Админ-панель",
                                         web_app=WebAppInfo(url=SUPPORT_APP_URL))])
    buttons.append([InlineKeyboardButton(text="📥 Все обращения",callback_data="staff:list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def support_admin_start(message: types.Message):
    if message.chat.type!="private" or not message.from_user:return
    staff=await telegram_staff(message.from_user.id)
    if not staff:
        code=await generate_pair_code(message.from_user.id)
        await message.answer(
            "🔐 Подключение Telegram-поддержки\n\n"
            "Войдите в веб-админку со своей учётной записью, откройте "
            "«Поддержка → Telegram» и введите код:\n\n"
            f"{code}\n\nКод действует 10 минут. Не передавайте его другим."
        )
        return
    try:
        await install_staff_menu(message.bot,message.chat.id)
    except Exception:
        log.exception("Unable to set staff Telegram menu button")
    await message.answer("🛟 Админ-панель · "+staff["username"]+
                         "\nОткройте полноценную мобильную панель кнопкой ниже "
                         "или через «Меню» внизу чата.",
                         reply_markup=app_keyboard())


async def support_admin_callback(query:types.CallbackQuery):
    if not query.from_user or not query.message or query.message.chat.type!="private":return
    staff=await telegram_staff(query.from_user.id)
    if not staff:
        await query.answer("Нет доступа. Подключите аккаунт через /supportadmin.",show_alert=True)
        return
    await query.answer()
    data=query.data or ""
    if data=="staff:list":
        tickets,total=await list_tickets(status="open")
        if not tickets:
            tickets,total=await list_tickets(status="pending")
        buttons=[[InlineKeyboardButton(
            text=f"#{t['id']} · {(t['first_name'] or t['username'] or str(t['telegram_id']))[:25]}",
            callback_data=f"staff:ticket:{t['id']}")] for t in tickets[:15]]
        buttons.append([InlineKeyboardButton(text="🔄 Обновить",callback_data="staff:list")])
        await query.message.answer(f"📥 Открытых обращений: {total}\n"
                                   "Показаны последние обращения, требующие внимания.",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif data.startswith("staff:ticket:"):
        try:ticket_id=int(data.split(":")[2])
        except (ValueError,IndexError):return
        ticket,messages=await ticket_detail(ticket_id)
        if not ticket:
            await query.message.answer("Обращение не найдено")
            return
        history=[]
        for m in messages[-8:]:
            if m.get("deleted_at"):continue
            content=m["body"] or {"photo":"📷 Фото","voice":"🎤 Голосовое",
                                   "document":"📎 Документ"}.get(m.get("media_type"),"Вложение")
            actor="Участник" if m["actor"]=="user" else "Поддержка"
            history.append(f"{actor}: {content[:350]}")
        text=(f"💬 Обращение #{ticket_id}\n"
              f"От: {ticket['first_name'] or ticket['username'] or ticket['telegram_id']}\n"
              f"Статус: {STATUSES.get(ticket['status'],ticket['status'])}\n\n"
              +("\n\n".join(history) or "Сообщений пока нет."))
        await query.message.answer(text[:3900],
                                   reply_markup=keyboard(ticket_id,ticket["status"]=="closed"))
    elif data.startswith("staff:reply:"):
        if not staff["can_reply"]:
            await query.message.answer("Нет права отвечать на обращения.")
            return
        try:ticket_id=int(data.split(":")[2])
        except (ValueError,IndexError):return
        try:await set_reply_target(query.from_user.id,ticket_id)
        except ValueError as exc:
            await query.message.answer(str(exc))
            return
        await query.message.answer(f"✍️ Ответ для обращения #{ticket_id}.\n"
                                   "Отправьте текст, фото, голосовое или документ следующим сообщением. "
                                   "Отмена: /cancelreply")
    elif data.startswith("staff:status:"):
        parts=data.split(":")
        if len(parts)!=4:return
        try:ticket_id=int(parts[2])
        except ValueError:return
        status=parts[3]
        if status not in ("open","pending","closed"):return
        if status=="closed" and not staff["can_close"]:
            await query.message.answer("Нет права закрывать обращения.")
            return
        if status!="closed" and not staff["can_change_status"]:
            await query.message.answer("Нет права изменять статус.")
            return
        try:
            recipient=await set_ticket_status(ticket_id,status)
            if status=="closed":
                try:
                    await query.bot.send_message(
                        recipient,
                        "✅ Ваш вопрос закрыт. Если нужна помощь, откройте чат поддержки снова.",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text="🛟 Поддержка",callback_data="help_menu")
                        ]])
                    )
                except Exception:
                    log.warning("Support closure notice failed ticket=%s",ticket_id)
        except ValueError as exc:
            await query.message.answer(str(exc))
            return
        await query.message.answer(f"✅ Статус обращения #{ticket_id}: {STATUSES[status]}",
                                   reply_markup=keyboard(ticket_id,status=="closed"))


async def support_admin_cancel(message:types.Message):
    if not message.from_user or message.chat.type!="private":return
    if not await telegram_staff(message.from_user.id):return
    await set_reply_target(message.from_user.id,None)
    await message.answer("Режим ответа выключен.",reply_markup=keyboard())


async def is_support_admin_reply(message:types.Message):
    if message.chat.type!="private" or not message.from_user:return False
    if message.text and message.text.startswith("/"):return False
    if not (message.text or message.photo or message.voice or message.document):return False
    staff=await telegram_staff(message.from_user.id)
    return bool(staff and staff["can_reply"] and staff["reply_ticket_id"])


async def support_admin_reply(message:types.Message):
    staff=await telegram_staff(message.from_user.id)
    if not staff or not staff["can_reply"] or not staff["reply_ticket_id"]:return
    ticket_id=staff["reply_ticket_id"]
    kind="text"
    file_id=None
    body=message.text or message.caption or ""
    if message.photo:
        kind="photo"
        file_id=message.photo[-1].file_id
    elif message.voice:
        kind="voice"
        file_id=message.voice.file_id
    elif message.document:
        kind="document"
        if message.document.file_size and message.document.file_size>20*1024*1024:
            await message.answer("Файл слишком большой (максимум 20 МБ).")
            return
        file_id=message.document.file_id
        body=body or (message.document.file_name or "Документ")
    try:
        uid,mid=await admin_reply(ticket_id,staff["username"],body,kind,file_id)
    except ValueError as exc:
        await set_reply_target(message.from_user.id,None)
        await message.answer(str(exc))
        return
    success=False
    sent_id=None
    try:
        if kind=="photo":
            delivered=await message.bot.send_photo(uid,file_id,caption=body[:1024] or None)
        elif kind=="document":
            delivered=await message.bot.send_document(uid,file_id,caption=body[:1024] or None)
        elif kind=="voice":
            delivered=await message.bot.send_voice(uid,file_id,caption=body[:1024] or None)
        else:
            delivered=await message.bot.send_message(uid,body,parse_mode=None)
        success=True
        sent_id=delivered.message_id
    except Exception:
        log.exception("Telegram staff support reply failed ticket=%s",ticket_id)
    await mark_delivery(mid,success,sent_id)
    await set_reply_target(message.from_user.id,None)
    if success:
        await message.answer(f"✅ Ответ по обращению #{ticket_id} доставлен и сохранён.",
                             reply_markup=keyboard(ticket_id))
    else:
        await message.answer(f"⚠️ Ответ по обращению #{ticket_id} не доставлен. "
                             "Он сохранён как ошибка в общей истории; повторите из веб-панели.",
                             reply_markup=keyboard(ticket_id))


async def support_admin_notifications_worker(bot):
    while True:
        try:
            pending=await pending_notifications()
            for item in pending:
                sent=False
                try:
                    body=item["body"] or {"photo":"📷 Фото","voice":"🎤 Голосовое",
                                        "document":"📎 Файл"}.get(item["media_type"],"Вложение")
                    name=item["first_name"] or item["username"] or str(item["user_id"])
                    await bot.send_message(
                        item["telegram_id"],
                        f"🛟 Новое сообщение · обращение #{item['ticket_id']}\n"
                        f"От: {name}\n\n{body[:2000]}",
                        reply_markup=keyboard(item["ticket_id"]))
                    if item["media_type"]!="text" and item["telegram_message_id"]:
                        try:
                            await bot.copy_message(item["telegram_id"],item["user_id"],
                                                   item["telegram_message_id"])
                        except Exception:
                            log.warning("Support attachment preview unavailable ticket=%s",item["ticket_id"])
                    sent=True
                except Exception:
                    log.exception("Staff support notification failed ticket=%s",item["ticket_id"])
                await mark_notification(item["admin_id"],item["message_id"],sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Telegram staff notification worker failed")
        await asyncio.sleep(5)
