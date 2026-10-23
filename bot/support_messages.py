"""Telegram-safe, human-friendly support responses."""
from html import escape
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def reply_keyboard(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Написать ответ", callback_data=f"help_reply:{ticket_id}"
        )],
        [InlineKeyboardButton(
            text="📂 История обращений", callback_data="help_list"
        )],
    ])


def format_staff_reply(ticket_id: int, body: str):
    return (
        "💬 <b>Вам ответила поддержка «Круга Души»</b>\n"
        f"<i>Обращение №{ticket_id}</i>\n\n"
        f"{escape(body.strip())}\n\n"
        "Если остались вопросы, напишите нам в этом же обращении."
    )
