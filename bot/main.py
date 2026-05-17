# import os
# from aiogram import Bot, Dispatcher
# from aiohttp import web
# from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
# from dotenv import load_dotenv
# from bot.handlers import start_handler
# from aiogram.filters import CommandStart

# load_dotenv()

# BOT_TOKEN = os.getenv("BOT_TOKEN")
# WEBHOOK_PATH = "/webhook"
# WEBHOOK_URL = "https://yourdomain.com/webhook"

# bot = Bot(token=BOT_TOKEN)
# dp = Dispatcher()

# dp.message.register(start_handler, CommandStart())

# async def on_startup(app):
#     await bot.set_webhook(WEBHOOK_URL)

# async def on_shutdown(app):
#     await bot.delete_webhook()

# def main():
#     app = web.Application()

#     SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
#     setup_application(app, dp, bot=bot)

#     app.on_startup.append(on_startup)
#     app.on_shutdown.append(on_shutdown)

#     web.run_app(app, host="0.0.0.0", port=3000)

# if __name__ == "__main__":
#     main()

import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from db.queries import add_user, get_settings

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def start_handler(message: Message):
    await add_user(message.from_user.id)

    settings = await get_settings()

    if settings:
        kb = InlineKeyboardBuilder()
        kb.button(text=settings.btn1_text, url=settings.btn1_url)
        kb.button(text=settings.btn2_text, url=settings.btn2_url)

        await message.answer_video(
            settings.video_url,
            caption=f"{settings.text1}\n\n{settings.text2}",
            reply_markup=kb.as_markup()
        )
    else:
        await message.answer("No settings found")


from db.database import engine
from db.models import Base


async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())