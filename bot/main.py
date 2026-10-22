import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from dotenv import load_dotenv

from bot.handlers import start_handler, accept_offer_handler
from db.flow import initialize_flow

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
dp.message.register(start_handler, CommandStart())
dp.callback_query.register(accept_offer_handler, F.data == "accept_offer")

async def main():
    await initialize_flow()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
