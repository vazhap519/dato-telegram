#!/usr/bin/env python3
"""Read-only preflight for zero-grace paid Tribute expiry kicks.

No Telegram calls and NO SQL writes. Expiry comes from paid_until in
the signed, recorded subscription, not from calendar guesses.
"""
import asyncio
from pathlib import Path
import sys
from dotenv import load_dotenv
from sqlalchemy import text

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
load_dotenv(ROOT / ".env")

from db.database import SessionLocal
from db.tribute_paid_removal import (
    PRODUCTION_OFFER_ID, eligible_expired_tribute_members,
    tribute_paid_removal_status,
)


async def main():
    status=await tribute_paid_removal_status()
    async with SessionLocal() as s:
        data=(await s.execute(text("""
            SELECT
              (SELECT enabled FROM tribute_auto_settings WHERE id=1) AS payments_enabled,
              (SELECT removal_enabled FROM subscription_automation_settings WHERE id=1)
                AS general_removals_enabled,
              (SELECT chat_id FROM telegram_group_settings WHERE id=1) AS chat_id,
              (SELECT COUNT(DISTINCT b.telegram_id)
               FROM tribute_auto_buyers b
               JOIN subscriptions sub ON sub.id=b.subscription_row_id
               WHERE b.offer_id=:offer AND sub.provider='tribute')
                AS known_paid_buyers,
              (SELECT COUNT(DISTINCT m.telegram_id)
               FROM subscription_managed_members m
               JOIN tribute_auto_buyers b
                 ON b.telegram_id=m.telegram_id AND b.offer_id=:offer
               JOIN subscriptions sub ON sub.id=b.subscription_row_id
               JOIN telegram_group_settings grp ON grp.id=1
               WHERE m.chat_id=grp.chat_id AND m.removed_at IS NULL
                 AND sub.status='active' AND sub.paid_until<=NOW())
                AS expired_managed_paid_before_safety_checks
        """),{"offer":PRODUCTION_OFFER_ID})).mappings().one()
    print("Real Tribe production offer:",PRODUCTION_OFFER_ID)
    print("Payments automatic:",bool(data["payments_enabled"]))
    print("Legacy general-removal flag:",bool(data["general_removals_enabled"]))
    print("Dedicated paid-expiry removal:",status["enabled"])
    print("Tracked paid, bot-invited members:",status["tracked_paid_members"])
    print("Recorded real-offer buyers:",data["known_paid_buyers"])
    print("Expired paid members before exclusions:",data["expired_managed_paid_before_safety_checks"])
    chat_id=data["chat_id"]
    if chat_id:
        candidates=await eligible_expired_tribute_members(chat_id,limit=1000)
        print("Due and safely eligible WHILE toggle is in its current state:",len(candidates))
    else:
        print("CHECK FAILED: protected Telegram group has no chat ID")
        return 1
    print("No grace period is applied; paid_until is the actual expiration timestamp.")
    print("No user was removed, invited, or changed by this check.")
    return 0


if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))
