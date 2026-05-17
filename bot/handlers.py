from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from db.queries import add_user, get_settings
from admin.websocket import notify_all


async def start_handler(message: types.Message):
    user_id = message.from_user.id

    # 👤 მომხმარებლის შენახვა
    await add_user(user_id)

    # 📡 dashboard notification
    await notify_all(f"New user: {user_id}")

    # ⚙️ settings DB-დან
    settings = await get_settings()

    # 🔍 DEBUG
    print("======== SETTINGS ========")
    if settings:
        print("VIDEO:", settings.video_url)
        print("TEXT1:", settings.text1)
        print("TEXT2:", settings.text2)
        print("BTN1:", settings.btn1_text, settings.btn1_url)
        print("BTN2:", settings.btn2_text, settings.btn2_url)
    else:
        print("SETTINGS: None")
    print("==========================")

    # ❗ თუ ცარიელია
    if not settings:
        await message.answer("⚠️ Settings not found")
        return

    # 🔘 ღილაკები
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=settings.btn1_text or "Button 1",
                url=settings.btn1_url or "#"
            ),
            InlineKeyboardButton(
                text=settings.btn2_text or "Button 2",
                url=settings.btn2_url or "#"
            )
        ]
    ])

    # 📝 ტექსტი
    caption_text = f"{settings.text1 or ''}\n\n{settings.text2 or ''}"

    # 🎬 ვიდეო გაგზავნა (try/except რომ არ დაექრეშოს)
    try:
        await message.answer_video(
            video=settings.video_url,
            caption=caption_text,
            reply_markup=keyboard
        )
    except Exception as e:
        print("❌ VIDEO ERROR:", e)

        # fallback (თუ ვიდეო არ იმუშავებს)
        await message.answer(
            f"{caption_text}\n\n⚠️ Video failed to load",
            reply_markup=keyboard
        )