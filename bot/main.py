import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

from bot.handlers import start_handler, accept_offer_handler, upload_command_handler, upload_video_note_handler, subscription_click_handler, choose_plan_handler, back_to_subscription_handler, my_subscription_command, my_subscription_callback
from db.flow import initialize_flow
from db.subscriptions import initialize_subscriptions
from db.broadcast import initialize_broadcasts
from db.automation import initialize_automation, enqueue_reminders
from bot.broadcast_worker import broadcast_worker
from bot.handlers import broadcast_optout_handler, broadcast_optin_handler, broadcast_stop_handler

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
dp.message.register(start_handler, CommandStart())
dp.message.register(my_subscription_command, Command('subscription', 'my_subscription'))
dp.message.register(upload_command_handler, Command('upload'))
dp.message.register(upload_video_note_handler, F.video_note)
dp.callback_query.register(accept_offer_handler, F.data == "accept_offer")
dp.callback_query.register(subscription_click_handler, F.data == "subscription_click")
dp.callback_query.register(choose_plan_handler, F.data.startswith("choose_plan:"))
dp.callback_query.register(back_to_subscription_handler, F.data == "back_to_subscription")
dp.callback_query.register(my_subscription_callback, F.data == "my_subscription")
dp.callback_query.register(broadcast_optout_handler, F.data == "broadcast_optout")
dp.message.register(broadcast_optin_handler, Command("notifications"))
dp.message.register(broadcast_stop_handler, Command("stopnotifications"))

async def main():
    await initialize_flow()
    await initialize_subscriptions()
    await initialize_broadcasts()
    await initialize_automation()
    async def reminder_scheduler():
        while True:
            try:
                await enqueue_reminders()
            except Exception:
                logging.exception("Reminder scheduler failure")
            await asyncio.sleep(60)
    reminders_task = asyncio.create_task(reminder_scheduler())
    task = asyncio.create_task(broadcast_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders_task.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
