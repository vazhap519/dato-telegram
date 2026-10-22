"""Scheduled entitlement reminders; no group kicks or payment changes."""
import asyncio
import logging
from db.expiry_monitor import claim_expiry_alerts, finish_expiry_alert
from db.subscription_automation import automation_settings

logger=logging.getLogger(__name__)

async def expiry_worker(bot):
    # Re-check every 30 seconds. Each user/end-date/stage can be sent only once.
    while True:
        try:
            if not (await automation_settings())["notifications_enabled"]:
                await asyncio.sleep(30)
                continue
            alerts=await claim_expiry_alerts()
            for alert in alerts:
                kind=alert["alert_kind"]
                if kind=="three_days":
                    msg=("⏳ Доступ в «Круг Души» скоро закончится. "
                         "До окончания осталось не более трёх дней. "
                         "Посмотреть статус и выбрать новый тариф можно через /subscription.")
                elif kind=="one_day":
                    msg=("⏳ Доступ в «Круг Души» заканчивается в ближайшие 24 часа. "
                         "Посмотреть статус подписки: /subscription.")
                else:
                    grace = (await automation_settings())["grace_hours"]
                    msg = (
                        "⏳ Срок подписки «Круг Души» закончился. "
                        + (f"Льготный период для продления: {grace} ч. "
                           if grace else "Льготного периода нет. ")
                        + "Если уже оплатили, обратитесь к администратору: "
                        "подтверждение Tribute пока не подключено. "
                        "Проверить подписку: /subscription."
                    )
                try:
                    await bot.send_message(alert["telegram_id"],msg)
                    await finish_expiry_alert(alert["id"],True)
                except Exception:
                    logger.exception("Expiry notification failed for %s",alert["telegram_id"])
                    await finish_expiry_alert(alert["id"],False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Expiry monitoring cycle failed")
        await asyncio.sleep(30)
