import asyncio
import logging
from db.admin_extensions import claim_due_tasks, log_task_notification, task_notification_settings

logger=logging.getLogger(__name__)

async def crm_task_worker(bot):
    while True:
        try:
            config=await task_notification_settings()
            if config["enabled"] and config["telegram_id"]:
                for task in await claim_due_tasks():
                    try:
                        await bot.send_message(
                            config["telegram_id"],
                            "📅 Задача CRM\n" + task["title"]
                            + "\nTelegram ID: " + str(task["telegram_id"])
                            + "\nОтветственный: " + task["assignee"],
                            parse_mode=None
                        )
                        await log_task_notification(task["id"],True)
                    except Exception as exc:
                        logger.warning("CRM alert failed task=%s",task["id"])
                        await log_task_notification(task["id"],False,type(exc).__name__)
        except Exception:
            logger.exception("CRM reminder worker error")
        await asyncio.sleep(60)
