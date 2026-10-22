import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

from bot.handlers import start_handler, accept_offer_handler, upload_command_handler, upload_video_note_handler, subscription_click_handler
from db.flow import initialize_flow

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
dp.message.register(start_handler, CommandStart())
dp.message.register(upload_command_handler, Command('upload'))
dp.message.register(upload_video_note_handler, F.video_note)
dp.callback_query.register(accept_offer_handler, F.data == "accept_offer")
dp.callback_query.register(subscription_click_handler, F.data == "subscription_click")

async def main():
    await initialize_flow()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
