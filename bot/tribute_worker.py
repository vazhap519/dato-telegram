"""Background Tribute payment consumer: durable inbox -> paid rights -> invite outbox.

The webhook HTTP endpoint itself does not grant access. Worker ignores every
unallowlisted offer and respects the owner-controlled global kill switch.
No membership removal or cancellation happens in this module.
"""
import asyncio
import logging

from db.tribute_auto import (
    AutoPaymentError, is_auto_processing_enabled, pending_auto_event_ids,
    activate_auto_event, mark_auto_event_attention, claim_due_invites,
    finish_invite,
)
from bot.group_handlers import send_group_invitation

log = logging.getLogger(__name__)


async def tribute_subscription_worker(bot):
    while True:
        try:
            if await is_auto_processing_enabled():
                ids = await pending_auto_event_ids(limit=20)
                for event_id in ids:
                    try:
                        result = await activate_auto_event(event_id)
                        if result == "processed":
                            log.info("Tribute payment recorded event_id=%s", event_id)
                    except AutoPaymentError as exc:
                        log.warning(
                            "Tribute payment requires review event_id=%s error=%s",
                            event_id, exc,
                        )
                        await mark_auto_event_attention(event_id, str(exc))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # Unexpected DB/infrastructure errors remain pending
                        # for the next worker iteration; never mark paid.
                        log.exception(
                            "Tribute payment consumer failed event_id=%s", event_id
                        )

                # Paid records are committed before Telegram is contacted.
                # A failed invite never revokes paid access; retry in 30 mins.
                if await is_auto_processing_enabled():
                    invites = await claim_due_invites(limit=5)
                    for job in invites:
                        event_id = job["event_id"]
                        uid = job["telegram_id"]
                        try:
                            result = await send_group_invitation(
                                bot, uid, "tribute_auto_payment"
                            )
                            if result in ("sent", "already_member"):
                                await finish_invite(event_id, True, result)
                            else:
                                await finish_invite(event_id, False, result)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.exception(
                                "Tribute invite failed; retry scheduled uid=%s event=%s",
                                uid, event_id,
                            )
                            try:
                                await finish_invite(
                                    event_id, False,
                                    "Telegram send or database failure"
                                )
                            except Exception:
                                log.exception(
                                    "Could not schedule Tribute invite retry id=%s",
                                    event_id
                                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Tribute automation worker loop failed")
        await asyncio.sleep(10)
