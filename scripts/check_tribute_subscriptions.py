#!/usr/bin/env python3
"""Read-only Tribute subscription catalogue check.

Uses the existing creator API key. Never creates orders, initiates
charges, changes channel settings, or modifies group access.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

API_URL = "https://tribute.tg/api/v1/subscriptions"


def parse_catalog(body: object) -> list[dict]:
    """The documented response is {'result': [subscriptions...]}; fail safe."""
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise ValueError("Unexpected subscription catalogue response")
    entries = []
    for item in body["result"]:
        if not isinstance(item, dict):
            continue
        subscription_id = item.get("subscriptionId")
        name = item.get("name")
        currency = item.get("currency")
        periods = item.get("periods")
        if type(subscription_id) is not int or not isinstance(periods, list):
            continue
        for period in periods:
            if not isinstance(period, dict):
                continue
            period_id = period.get("periodId")
            cadence = period.get("period")
            price = period.get("price")
            if (
                type(period_id) is not int
                or cadence not in (
                    "trial", "onetime", "weekly", "monthly",
                    "quarterly", "halfyearly", "yearly"
                )
                or type(price) not in (float, int)
                or isinstance(price, bool)
            ):
                continue
            entries.append({
                "subscription_id": subscription_id,
                "period_id": period_id,
                "name": str(name or "")[:100],
                "currency": str(currency or "")[:12],
                "cadence": cadence,
                "price": price,
            })
    return entries


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("TRIBUTE_API_KEY", "").strip()
    if not key:
        print("API key not found in .env. No request sent.")
        return 2
    request = Request(API_URL, headers={
        "Api-Key": key,
        "Accept": "application/json",
        "User-Agent": "dato-tribute-subscription-catalog-check/1",
    }, method="GET")
    try:
        with urlopen(request, timeout=15) as response:
            if response.status != 200:
                print("Unexpected API HTTP status:", response.status)
                return 1
            body = json.loads(response.read(262145).decode("utf-8"))
        items = parse_catalog(body)
    except HTTPError as exc:
        print("Tribute catalogue API returned HTTP", exc.code)
        print("No changes made.")
        return 1
    except (URLError, TimeoutError, OSError, UnicodeDecodeError, ValueError) as exc:
        print("Tribute catalogue check failed:", type(exc).__name__)
        return 1

    print("GET /subscriptions: HTTP 200")
    print("Available pricing periods:", len(items))
    for item in items[:30]:
        print(
            f"subscriptionId={item['subscription_id']} "
            f"periodId={item['period_id']} "
            f"period={item['cadence']} "
            f"price={item['price']} {item['currency']} "
            f"name={item['name']}"
        )
    if len(items) > 30:
        print("Additional pricing periods omitted:", len(items) - 30)
    matches = [
        item for item in items
        if item["currency"].upper() == "USD"
        and item["cadence"] == "monthly"
        and float(item["price"]) == 1
    ]
    print("USD 1 monthly candidates:", len(matches))
    print("Note: this catalogue has no payment URL or buyer records; match the test subscription manually.")
    print("Read-only check complete. No charges, payment settings or access records changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
