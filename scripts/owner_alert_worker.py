#!/usr/bin/env python3
"""Single-run owner alerts (systemd timer), never starts polling.

Usage:
  python -m scripts.owner_alert_worker --dry-run
  python -m scripts.owner_alert_worker --run
"""
import argparse
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path("/opt/dato-telegram")
logging.basicConfig(level=logging.INFO)

async def main(send):
    if not load_dotenv(ROOT/".env"):
        raise RuntimeError("Runtime .env unavailable")
    from db.owner_alerts import deliver
    if not send:
        # No Bot object necessary for dry-run, and the database stays
        # untouched, including delivery claims.
        result=await deliver(None,dry_run=True)
    else:
        import os
        from aiogram import Bot
        if not os.getenv("BOT_TOKEN"):
            raise RuntimeError("BOT_TOKEN missing")
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            result=await deliver(bot)
    print("OWNER ALERTS",result,flush=True)
    if result["errors"]:
        raise RuntimeError("Some Telegram notifications could not be delivered")

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    run=parser.add_mutually_exclusive_group(required=True)
    run.add_argument("--dry-run",action="store_true")
    run.add_argument("--run",action="store_true")
    args=parser.parse_args()
    asyncio.run(main(args.run))
