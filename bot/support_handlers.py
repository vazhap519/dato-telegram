"""Private Telegram support flow with menu, categories and ticket history."""
import logging
from aiogram import types
from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
from db.support import (CATEGORIES,STATUSES,start_draft,cancel_draft,save_user_message,
                        draft_exists,user_tickets,ticket_detail)

log=logging.getLogger(__name__)

def menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Новое обращение",callback_data="help_new")],
        [InlineKeyboardButton(text="📂 Мои обращения",callback_data="help_list")],
        [InlineKeyboardButton(text="👤 Моя подписка",callback_data="my_subscription")],
    ])

async def support_start(message:types.Message):
    if message.chat.type!="private": return
    await message.answer("🛟 Помощник «Круга Души»\n\nЗдесь можно задать вопрос о доступе, оплате или работе бота. Ваше обращение получит номер, а ответ придёт сюда в Telegram.",reply_markup=menu())

async def support_callback(query:types.CallbackQuery):
    uid=query.from_user.id
    if not query.message or query.message.chat.type!="private": 
        await query.answer()
        return
    data=query.data or ""
    await query.answer()
    if data=="help_menu":
        await cancel_draft(uid)
        await query.message.answer("🛟 Чем помочь?",reply_markup=menu())
    elif data=="help_new":
        await query.message.answer("Выберите тему обращения:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=label,callback_data="help_cat:"+key)]
            for key,label in CATEGORIES.items()
        ]+[[InlineKeyboardButton(text="↩️ Назад",callback_data="help_menu")]]))
    elif data.startswith("help_cat:"):
        category=data.split(":",1)[1]
        if category not in CATEGORIES: return
        await start_draft(uid,category=category)
        await query.message.answer(
            "📝 "+CATEGORIES[category]+"\n\nНапишите одним сообщением, что случилось. "
            "Укажите важные детали, но не отправляйте пароли, коды или данные банковской карты. "
            "Максимум 3000 символов.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена",callback_data="help_menu")
            ]]))
    elif data=="help_list":
        tickets=await user_tickets(uid)
        if not tickets:
            await query.message.answer("Пока нет обращений.",reply_markup=menu())
            return
        rows=[[InlineKeyboardButton(text=f"#{t['id']} · {CATEGORIES.get(t['category'],t['category'])} · {STATUSES.get(t['status'],t['status'])}",callback_data=f"help_ticket:{t['id']}")] for t in tickets]
        rows.append([InlineKeyboardButton(text="➕ Новое обращение",callback_data="help_new")])
        await query.message.answer("📂 Ваши последние обращения:",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    elif data.startswith("help_ticket:"):
        try: ticket_id=int(data.split(":",1)[1])
        except ValueError:return
        ticket,msgs=await ticket_detail(ticket_id,uid=uid)
        if not ticket:
            await query.message.answer("Обращение не найдено.")
            return
        history="\n".join(("👤 Вы: " if m["actor"]=="user" else "🛟 Поддержка: ")+m["body"][:500] for m in msgs[-8:])
        lines=[f"📨 Обращение #{ticket_id}",CATEGORIES.get(ticket["category"],ticket["category"]),
               "Статус: "+STATUSES.get(ticket["status"],ticket["status"]),"",history or "Сообщений нет."]
        if len("\n".join(lines))>3500:
            lines=lines[:4]+["\n".join(lines[4:])[-3000:]]
        buttons=[]
        if ticket["status"]!="closed":
            buttons.append([InlineKeyboardButton(text="✍️ Дополнить обращение",callback_data=f"help_reply:{ticket_id}")])
        buttons.append([InlineKeyboardButton(text="📂 Все обращения",callback_data="help_list")])
        await query.message.answer("\n".join(lines),reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    elif data.startswith("help_reply:"):
        try: ticket_id=int(data.split(":",1)[1])
        except ValueError:return
        try: await start_draft(uid,ticket_id=ticket_id)
        except ValueError:
            await query.message.answer("Обращение закрыто. Создайте новое.",reply_markup=menu())
            return
        await query.message.answer(f"✍️ Напишите дополнение к обращению #{ticket_id} (до 3000 символов).",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена",callback_data="help_menu")
        ]]))

async def support_text(message:types.Message):
    if message.chat.type!="private" or not message.from_user:return
    uid=message.from_user.id
    if not await draft_exists(uid):return
    if not message.text or message.text.startswith("/"):
        await message.answer("Пришлите текст сообщения, либо нажмите «Отмена».")
        return
    try:
        tid=await save_user_message(uid,message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    if tid:
        await message.answer(f"✅ Сообщение принято! Обращение #{tid}.\n\nОтвет поддержки появится здесь. Вы можете дополнить обращение через «Мои обращения».",reply_markup=menu())
