#!/usr/bin/env python3
"""Provision four OFF-by-default Tribute 279348 mappings with ONE checkout URL.

Default mode is read-only. --apply performs the *explicitly requested* setup,
but does NOT enable processing, initiate a payment, invite or remove anybody.
Requires a fresh API catalogue match and the signed test payment's public
channel ID to guard against accidentally wiring a private Tribute group.
"""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from sqlalchemy import text
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
load_dotenv(ROOT / ".env")
from db.database import SessionLocal
from db.tribute_shared_checkout import (
    PERIODS, PUBLIC_CHANNEL_ID, PRODUCTION_OFFER_ID, PRODUCTION_URL,
    initialize_shared_checkout,
)
from scripts.check_tribute_subscriptions import API_URL, parse_catalog


def verify_catalogue():
    key=os.environ.get("TRIBUTE_API_KEY","").strip()
    if not key:
        raise ValueError("Tribute API key unavailable")
    req=Request(API_URL,headers={"Api-Key":key,"Accept":"application/json"},method="GET")
    try:
        with urlopen(req,timeout=15) as response:
            rows=parse_catalog(json.loads(response.read(262145).decode("utf-8")))
    except (HTTPError,URLError,OSError,ValueError,UnicodeDecodeError) as exc:
        raise ValueError("Unable to verify live Tribute subscription catalogue") from exc
    ours=[r for r in rows if r["subscription_id"]==PRODUCTION_OFFER_ID]
    if len(ours)!=4 or {x["period_id"] for x in ours}!={x[0] for x in PERIODS}:
        raise ValueError("Tribute offer/period IDs do not match the four expected periods")
    for pid,cadence,cents,days,label in PERIODS:
        item=next(x for x in ours if x["period_id"]==pid)
        if (item["currency"].lower()!="eur" or item["cadence"]!=cadence or
            Decimal(str(item["price"]))!=Decimal(cents)/100 or
            item["name"]!="Круг Души — закрытое сообщество"):
            raise ValueError("Price, period or offer name mismatch in live Tribute API")
    return ours


async def verify_channel():
    """The signed $1 test payment was tied to the same public channel on screen."""
    async with SessionLocal() as session:
        return (await session.execute(text("""
            SELECT EXISTS(
                SELECT 1 FROM payment_webhook_events
                WHERE provider='tribute' AND signature_valid=TRUE
                  AND payload->>'name'='new_subscription'
                  AND payload->'details'->>'subscription_id'='279302'
                  AND payload->'details'->>'channel_id'=:channel
                  AND status='processed'
            )
        """),{"channel":str(PUBLIC_CHANNEL_ID)})).scalar_one()


async def apply_setup():
    """Atomic, idempotent preparation; all mappings remain DISABLED."""
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("SELECT pg_advisory_xact_lock(638492702)"))
            global_enabled=(await s.execute(text("""
                SELECT enabled FROM tribute_auto_settings WHERE id=1
            """))).scalar_one_or_none()
            checkout=(await s.execute(text("""
                SELECT subscription_id,checkout_url,enabled
                FROM tribute_shared_checkout_settings WHERE id=1 FOR UPDATE
            """))).mappings().first()
            if not checkout:
                raise ValueError("Upgrade both services before provisioning checkout")
            if global_enabled or checkout["enabled"]:
                raise ValueError("Turn off global and shared checkout switches before preparing")
            if checkout["subscription_id"]!=PRODUCTION_OFFER_ID or checkout["checkout_url"]!=PRODUCTION_URL:
                raise ValueError("The configured checkout URL/offer differs from the approved one")
            current=(await s.execute(text("""
                SELECT COUNT(*) FROM tribute_auto_offers
                WHERE subscription_id=:offer AND enabled=TRUE
            """),{"offer":PRODUCTION_OFFER_ID})).scalar_one()
            if current:
                raise ValueError("An existing mapped period is already enabled; no automatic modifications")

            for pid,cadence,cents,days,label in PERIODS:
                code=f"circle_shared_{PRODUCTION_OFFER_ID}_{cadence}"
                plan=(await s.execute(text("""
                    INSERT INTO subscription_plans(
                        code,name,price_amount,currency,duration_days,is_lifetime,
                        is_active,payment_url
                    ) VALUES(:code,:name,:price,'EUR',:days,FALSE,TRUE,:url)
                    ON CONFLICT(code) DO NOTHING
                    RETURNING id
                """),{"code":code,"name":"Круг Души — "+label,
                        "price":Decimal(cents)/100,"days":days,
                        "url":PRODUCTION_URL})).scalar_one_or_none()
                row=(await s.execute(text("""
                    SELECT id,price_amount,currency,duration_days,is_lifetime,
                           is_active,archived_at,payment_url
                    FROM subscription_plans WHERE code=:code FOR UPDATE
                """),{"code":code})).mappings().one()
                if (not row["is_active"] or row["is_lifetime"] or row["archived_at"] is not None
                    or row["currency"]!="EUR" or row["duration_days"]!=days
                    or row["price_amount"] is None
                    or Decimal(row["price_amount"])!=Decimal(cents)/100
                    or row["payment_url"]!=PRODUCTION_URL):
                    raise ValueError("Existing bot plan conflicts with verified Tribute period "+str(pid))
                await s.execute(text("""
                    INSERT INTO tribute_auto_offers(
                        subscription_id,period_id,channel_id,amount_minor,
                        currency,period,plan_id,require_test_claim,enabled
                    ) VALUES(:offer,:pid,:channel,:cents,'eur',:cadence,:plan,FALSE,FALSE)
                    ON CONFLICT(subscription_id,period_id) DO NOTHING
                """),{"offer":PRODUCTION_OFFER_ID,"pid":pid,
                        "channel":PUBLIC_CHANNEL_ID,"cents":cents,
                        "cadence":cadence,"plan":row["id"]})
                linked=(await s.execute(text("""
                    SELECT * FROM tribute_auto_offers
                    WHERE subscription_id=:offer AND period_id=:pid FOR UPDATE
                """),{"offer":PRODUCTION_OFFER_ID,"pid":pid})).mappings().one()
                if (linked["enabled"] or linked["channel_id"]!=PUBLIC_CHANNEL_ID or
                    linked["amount_minor"]!=cents or linked["currency"]!="eur" or
                    linked["period"]!=cadence or linked["plan_id"]!=row["id"] or
                    linked["require_test_claim"]):
                    raise ValueError("Existing Tribute mapping conflicts with verified setup "+str(pid))
                print("Prepared OFF:",label,str(Decimal(cents)/100)+" EUR",
                      "period_id",pid,"plan_id",row["id"])
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES('tribute_shared_checkout_provisioned','owner_cli',
                       jsonb_build_object('offer_id',CAST(:offer AS BIGINT),
                                          'periods',4,'automatic',FALSE))
            """),{"offer":PRODUCTION_OFFER_ID})
    print("Prepared four mappings; all remain OFF. No payments or group changes.")


async def main(apply):
    print("Checking new shared checkout",PRODUCTION_OFFER_ID,
          "against the live Tribute catalogue (read-only).")
    data=verify_catalogue()
    print("Tribute API verified four EUR periods:")
    for x in data:
        print(x["period_id"],x["cadence"],x["price"],x["currency"])
    verified=await verify_channel()
    if not verified:
        print("STOP: Original public channel cannot be verified from a signed real event")
        return 2
    print("Public channel ID independently recorded in signed test webhook:",PUBLIC_CHANNEL_ID)
    if not apply:
        print("DRY RUN ONLY: No database changes. Use --apply after deployment if approved.")
        return 0
    try:
        await initialize_shared_checkout()
        await apply_setup()
        return 0
    except ValueError as exc:
        print("SAFE STOP:",str(exc))
        return 1


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--apply",action="store_true",
                        help="Explicitly prepare four OFF-by-default mappings")
    args=parser.parse_args()
    try:
        raise SystemExit(asyncio.run(main(args.apply)))
    except ValueError as exc:
        print("SAFE STOP:",str(exc))
        raise SystemExit(1)
