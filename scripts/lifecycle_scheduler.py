#!/usr/bin/env python3
"""Lifecycle queue producer. DEFAULT = read-only preview.

Explicit --enqueue schedules only eligible new events into the existing
broadcast queue; the pinned Telegram polling bot performs actual sends.
No Telegram token or direct send required by this process.
"""
import argparse
import asyncio
from dotenv import load_dotenv

async def run(execute=False):
    load_dotenv('/opt/dato-telegram/.env',override=True)
    from db.lifecycle_followups import enqueue_lifecycle
    result=await enqueue_lifecycle(limit=20,dry_run_only=not execute)
    print(("LIFECYCLE ENQUEUED" if execute else "LIFECYCLE DRY RUN"),result,flush=True)

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--enqueue",action="store_true")
    args=parser.parse_args()
    asyncio.run(run(args.enqueue))
