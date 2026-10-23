"""Simple support chat inside a private conversation with the bot."""
import logging
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.support import (start_support_chat, stop_support_chat, chat_is_active,
                        add_support_chat_message, user_tickets, ticket_detail)

log=logging.getLogger(__name__)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать в поддержку",callback_data="help_chat")],
        [InlineKeyboardButton(text="📂 История переписки",callback_data="help_list")],
        [InlineKeyboardButton(text="👤 Моя подписка",callback_data="my_subscription")],
    ])

def chat_controls():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Завершить разговор",callback_data="help_exit")],
        [InlineKeyboardButton(text="📂 История",callback_data="help_list")],
    ])

async def register_contact(who):
    from db.flow import register_user
    await register_user(who.id,username=who.username,
                        first_name=who.first_name,last_name=who.last_name)

async def support_start(message:types.Message):
    if message.chat.type!="private" or not message.from_user:return
    await register_contact(message.from_user)
    await message.answer("🛟 Поддержка «Круга Души»\n\nНапишите нам прямо здесь — ответ придёт в этот чат. Можно отправлять текст, фото и голосовые сообщения.",reply_markup=menu())

async def support_callback(query:types.CallbackQuery):
    if not query.message or query.message.chat.type!="private":return
    await register_contact(query.from_user)
    await query.answer()
    uid=query.from_user.id
    data=query.data or ""
    if data in ("help_chat","help_new","help_menu") or data.startswith("help_cat:"):
        if data=="help_menu":
            await query.message.answer("🛟 Поддержка «Круга Души»",reply_markup=menu())
            return
        await start_support_chat(uid)
        await query.message.answer(
            "💬 Чат поддержки открыт.\n\nПишите сюда в любое время — даже если мы ответим не сразу. Все сообщения сохраняются и попадут в переписку.\n\nЧтобы отключить приём сообщений поддержкой, нажмите «Завершить разговор».",
            reply_markup=chat_controls())
    elif data=="help_exit":
        await stop_support_chat(uid)
        await query.message.answer("✅ Вы вышли из чата поддержки. История сохранена. Чтобы снова написать, нажмите «Написать в поддержку».",reply_markup=menu())
    elif data=="help_list":
        tickets=await user_tickets(uid)
        if not tickets:
            await query.message.answer("История пока пуста.",reply_markup=menu())
            return
        rows=[[InlineKeyboardButton(
            text=f"💬 Диалог #{t['id']} · {('Закрыт' if t['status']=='closed' else 'Активен')}",
            callback_data=f"help_ticket:{t['id']}")] for t in tickets]
        rows.append([InlineKeyboardButton(text="💬 Написать",callback_data="help_chat")])
        await query.message.answer("📂 История переписки:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif data.startswith("help_ticket:"):
        try:tid=int(data.split(":",1)[1])
        except ValueError:return
        ticket,messages=await ticket_detail(tid,uid=uid)
        if not ticket:
            await query.message.answer("Диалог не найден.")
            return
        history=[]
        for m in messages[-8:]:
            prefix="Вы" if m["actor"]=="user" else "Поддержка"
            content=m["body"] or {"photo":"📷 Фото","voice":"🎤 Голосовое","document":"📎 Файл"}.get(m.get("media_type"),"Вложение")
            history.append(f"{prefix}: {content[:400]}")
        buttons=[]
        if ticket["status"]!="closed":
            buttons.append([InlineKeyboardButton(text="💬 Продолжить диалог",callback_data=f"help_reply:{tid}")])
        buttons.append([InlineKeyboardButton(text="← История",callback_data="help_list")])
        await query.message.answer("💬 Диалог #"+str(tid)+"\n\n"+("\n\n".join(history) or "Нет сообщений.")[:3400],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif data.startswith("help_reply:"):
        try:tid=int(data.split(":",1)[1])
        except ValueError:return
        try:await start_support_chat(uid,ticket_id=tid)
        except ValueError:
            await query.message.answer("Диалог закрыт. Откройте новый.",reply_markup=menu())
            return
        await query.message.answer("💬 Пишите сообщение — оно сразу попадёт в переписку.",reply_markup=chat_controls())

async def support_text(message:types.Message):
    if message.chat.type!="private" or not message.from_user:return
    uid=message.from_user.id
    if not await chat_is_active(uid):return
    if message.text and message.text.startswith("/"):return
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
            await message.answer("Файл слишком большой. Максимальный размер — 20 МБ.")
            return
        file_id=message.document.file_id
        body=body or (message.document.file_name or "Документ")
    elif not message.text:
        await message.answer("Пока можно отправлять текст, фотографии, голосовые и документы.")
        return
    try:
        ticket_id=await add_support_chat_message(uid,body,kind,file_id)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if ticket_id:
        # The user's own message remains in their Telegram chat; no repetitive confirmation.
        log.info("Support chat message saved uid=%s ticket=%s kind=%s",uid,ticket_id,kind)
