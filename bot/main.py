import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from dotenv import load_dotenv

from bot.handlers import start_handler, accept_offer_handler, upload_command_handler, upload_video_note_handler, subscription_click_handler, choose_plan_handler, back_to_subscription_handler, my_subscription_command, my_subscription_callback, request_group_invite_handler
from db.support import initialize_support
from bot.support_handlers import support_start, support_callback, support_text
from db.flow import initialize_flow
from db.subscriptions import initialize_subscriptions
from db.group_access import initialize_group_access
from db.expiry_monitor import initialize_expiry_monitor
from db.subscription_automation import initialize_subscription_automation
from bot.expiry_worker import expiry_worker
from bot.group_removal_worker import group_removal_test_worker
from bot.group_handlers import group_join_request_handler, group_id_handler, managed_member_join_handler
from db.broadcast import initialize_broadcasts
from db.automation import initialize_automation, enqueue_reminders
from bot.broadcast_worker import broadcast_worker
from bot.handlers import broadcast_optout_handler, broadcast_optin_handler, broadcast_stop_handler

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.environ["BOT_TOKEN"])
dp = Dispatcher()
dp.chat_join_request.register(group_join_request_handler)
dp.chat_member.register(managed_member_join_handler)
dp.message.register(start_handler, CommandStart())
dp.message.register(support_start, Command("help", "support"))
dp.callback_query.register(support_callback, F.data.startswith("help_"))
dp.message.register(group_id_handler, Command("chatid"))
dp.message.register(my_subscription_command, Command('subscription', 'my_subscription'))
dp.message.register(upload_command_handler, Command('upload'))
dp.message.register(upload_video_note_handler, F.video_note)
dp.callback_query.register(accept_offer_handler, F.data == "accept_offer")
dp.callback_query.register(subscription_click_handler, F.data == "subscription_click")
dp.callback_query.register(choose_plan_handler, F.data.startswith("choose_plan:"))
dp.callback_query.register(back_to_subscription_handler, F.data == "back_to_subscription")
dp.callback_query.register(my_subscription_callback, F.data == "my_subscription")
dp.callback_query.register(request_group_invite_handler, F.data == "request_group_invite")
dp.callback_query.register(broadcast_optout_handler, F.data == "broadcast_optout")
dp.message.register(broadcast_optin_handler, Command("notifications"))
dp.message.register(broadcast_stop_handler, Command("stopnotifications"))
dp.message.register(support_text)

async def main():
    await initialize_flow()
    await initialize_support()
    await initialize_subscriptions()
    await initialize_group_access()
    await initialize_expiry_monitor()
    await initialize_subscription_automation()
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
    expiry_task = asyncio.create_task(expiry_worker(bot))
    group_test_task = asyncio.create_task(group_removal_test_worker(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders_task.cancel()
        task.cancel()
        expiry_task.cancel()
        group_test_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        try:
            await group_test_task
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    asyncio.run(main())
