#!/usr/bin/env python3
"""Safe end-to-end Tribute webhook probe. Does NOT create subscriptions or access."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

URL = "https://bot.arutyun.me/webhooks/tribute"
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def send(raw: bytes, signature: str) -> tuple[int, dict]:
    req = Request(
        URL,
        data=raw,
        headers={"Content-Type": "application/json", "trbt-signature": signature},
        method="POST",
    )
    try:
        with urlopen(req, timeout=12) as response:
            status, payload = response.status, response.read(1024)
    except HTTPError as exc:
        status, payload = exc.code, exc.read(1024)
    try:
        data = json.loads(payload)
        return status, data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return status, {}


def main() -> int:
    key = os.getenv("TRIBUTE_API_KEY", "").strip()
    if not key:
        print("NOT CONFIGURED: TRIBUTE_API_KEY is not present in the project's .env.")
        print("No request was sent. Do not paste your secret into the chat.")
        return 2
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    probe = {
        "name": "integration_probe",
        "created_at": now,
        "sent_at": now,
        "payload": {"probe_id": secrets.token_hex(12)},
    }
    raw = json.dumps(probe, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    try:
        first, first_data = send(raw, signature)
        second, second_data = send(raw, signature)
        invalid, invalid_data = send(raw + b" ", signature)
    except (URLError, TimeoutError, OSError) as exc:
        print("NETWORK ERROR:", type(exc).__name__)
        return 3

    print("Signed request:", first, first_data.get("status", first_data.get("detail", "-")))
    print("Repeated request:", second, second_data.get("status", second_data.get("detail", "-")))
    print("Altered request:", invalid, invalid_data.get("detail", "-"))
    success = (
        first == 200 and first_data.get("status") == "received"
        and first_data.get("access_changes") is False
        and second == 200 and second_data.get("status") == "duplicate"
        and second_data.get("access_changes") is False
        and invalid == 401
    )
    print("RESULT:", "PASS" if success else "FAIL — review the endpoint and service configuration")
    print("No Telegram group permissions or subscription records were changed by this probe.")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
