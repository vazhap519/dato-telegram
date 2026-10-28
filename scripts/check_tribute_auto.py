#!/usr/bin/env python3
"""Read-only end-to-end preflight for Tribute automatic grants.

No payments are initiated, no webhooks replayed, no DB writes and no Telegram
invites/removals. Compares exact DB offer settings with Tribute's own catalogue.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
load_dotenv(ROOT / ".env")

from db.tribute_auto import list_auto_settings, validate_paid_event, AutoPaymentError
from db.database import SessionLocal
from scripts.check_tribute_subscriptions import parse_catalog
from sqlalchemy import text


def fetch_live_catalogue():
    key=os.environ.get("TRIBUTE_API_KEY","").strip()
    if not key:
        print("Tribute API key is not configured; catalogue check skipped")
        return None
    request=Request("https://tribute.tg/api/v1/subscriptions",
                    headers={"Api-Key":key,"Accept":"application/json"},
                    method="GET")
    try:
        with urlopen(request,timeout=15) as response:
            return parse_catalog(json.loads(response.read(262145).decode("utf-8")))
    except (HTTPError,URLError,OSError,ValueError,UnicodeDecodeError) as exc:
        print("Tribute catalogue unavailable:",type(exc).__name__)
        return None


async def verify_db():
    state=await list_auto_settings()
    print("Global auto payment processing:", "ON" if state["setting"]["enabled"] else "OFF (safe)")
    print("Configured exact offers:",len(state["offers"]))
    print("Unsigned or manual-review backlog:",state["pending_total"])
    print("Invite outbox by status:",state["outbox"])

    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT id,status,signature_valid,payload
            FROM payment_webhook_events
            WHERE provider='tribute'
              AND payload->>'name'='new_subscription'
              AND signature_valid=TRUE
            ORDER BY id DESC LIMIT 30
        """))).mappings().all()
    for offer in state["offers"]:
        enabled=offer["enabled"]
        print("\nOffer ID:",offer["subscription_id"],
              "| Period ID:",offer["period_id"],
              "| plan:",offer["plan_name"],
              "| approval:",("ON" if enabled else "OFF"),
              "| price minor units:",offer["amount_minor"],offer["currency"],
              "| test-only:",offer["require_test_claim"])
        matches=[x for x in rows
                 if isinstance(x["payload"],dict)
                 and x["payload"].get("details",{}).get("subscription_id")==offer["subscription_id"]
                 and x["payload"].get("details",{}).get("period_id")==offer["period_id"]]
        for e in matches[:2]:
            try:
                result=validate_paid_event(e["payload"],offer)
                print(" Signed real event:",e["id"],"status:",e["status"],
                      "valid until:",result["expires_at"].isoformat())
            except AutoPaymentError as exc:
                print(" Signed event needs review:",e["id"],str(exc))
    print("\nRead-only DB preflight completed.")
    return state


async def main():
    state=await verify_db()
    catalogue=fetch_live_catalogue()
    if catalogue is None:
        print("CHECK FAILED: live Tribute catalogue is unavailable. Keep automation OFF.")
        return 2
    problems=0
    for offer in state["offers"]:
        match=[x for x in catalogue
               if x["subscription_id"]==offer["subscription_id"]
               and x["period_id"]==offer["period_id"]]
        if len(match)!=1:
            problems+=1
            print("ERROR: Offer/period not found in Tribute API:",
                  offer["subscription_id"],offer["period_id"])
            continue
        row=match[0]
        amount_minor=round(float(row["price"])*100)
        okay=(row["currency"].lower()==offer["currency"]
              and row["cadence"]==offer["period"]
              and amount_minor==offer["amount_minor"])
        if not okay:
            problems+=1
        print("Tribute API match:",offer["subscription_id"],
              "period:",offer["period_id"],
              "result:",("OK" if okay else "MISMATCH"))
    print("RESULT:",("PASS (read-only)" if problems==0 else "CHECK FAILED"))
    print("No user grants, removals or payments were created.")
    return 0 if problems==0 else 1


if __name__=="__main__":
    raise SystemExit(asyncio.run(main()))
