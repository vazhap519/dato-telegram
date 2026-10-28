#!/usr/bin/env python3
"""Read-only check before activating an already-paid Tribute test subscription.

This script NEVER grants/revokes access, invites users, or charges a card.
It deliberately does not print secrets, signatures or customer information.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys

from dotenv import load_dotenv
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from db.database import SessionLocal
from db.tribute_test_activation import (
    TEST_CHANNEL_ID, TributeActivationError, validate_test_purchase,
)
from db.tribute_payment_test import TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID


async def main():
    async with SessionLocal() as session:
        events = (await session.execute(text("""
            SELECT id,status,signature_valid,payload,received_at
            FROM payment_webhook_events
            WHERE provider='tribute'
              AND payload->>'name'='new_subscription'
              AND payload->'details'->>'subscription_id'=:offer
            ORDER BY id DESC LIMIT 5
        """), {"offer": str(TEST_SUBSCRIPTION_ID)})).mappings().all()

        print("Tribute test offer:", TEST_SUBSCRIPTION_ID,
              "period:", TEST_PERIOD_ID, "channel:", TEST_CHANNEL_ID)
        print("Stored payment events for this offer:", len(events))
        if not events:
            print("CHECK: No recorded purchase event, do not approve payment.")
            return 1

        for row in events:
            print("\nJournal event ID:", row["id"],
                  "| Status:", row["status"],
                  "| Signature verified:", row["signature_valid"])
            if not row["signature_valid"]:
                print("CHECK: Unsigned event, do not approve.")
                continue
            try:
                payment = validate_test_purchase(row["payload"])
            except TributeActivationError as exc:
                print("CHECK: Invalid purchase:", str(exc))
                continue

            uid = payment["telegram_id"]
            known_user = (await session.execute(text("""
                SELECT EXISTS(SELECT 1 FROM users WHERE telegram_id=:uid)
            """), {"uid": uid})).scalar_one()
            linked_test = (await session.execute(text("""
                SELECT EXISTS(
                    SELECT 1 FROM tribute_payment_test_links
                    WHERE claimed_by=:uid AND claimed_at IS NOT NULL
                      AND claimed_at<=:received
                      AND claimed_at>=:earliest
                )
            """), {"uid":uid, "received":row["received_at"],
                    "earliest":row["received_at"]-timedelta(hours=2)})).scalar_one()
            print("Telegram ID (from signed Tribute event):", uid)
            print("Bot user exists:", bool(known_user))
            print("Private test link claimed by same Telegram ID:", bool(linked_test))
            print("Payment valid until (UTC):", payment["paid_until"].isoformat())

            if known_user and linked_test:
                print("CHECK: Purchase is eligible for admin confirmation.")
            else:
                print("CHECK: Investigate user/link mismatch. Do not pay again.")

        group = (await session.execute(text("""
            SELECT chat_id,chat_title FROM telegram_group_settings WHERE id=1
        """))).mappings().first()
        print("\nPrivate group configured:", bool(group and group["chat_id"]))
        print("Configured group name:", (group or {}).get("chat_title") or "—")
        settings = (await session.execute(text("""
            SELECT removal_enabled FROM subscription_automation_settings WHERE id=1
        """))).mappings().first()
        print("Managed-member auto-removal toggle:", bool(settings and settings["removal_enabled"]))
        print("Read-only preflight complete. No entitlement or group changes.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
