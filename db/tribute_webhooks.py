"""Verified Tribute webhook inbox. Stage 1 never modifies subscriber access."""
import base64
import hashlib
import hmac
import json
from datetime import datetime

MAX_BODY_BYTES = 65536
SUBSCRIPTION_EVENTS = frozenset({
    "new_subscription", "renewed_subscription", "cancelled_subscription",
})
SAFE_FIELDS = frozenset({
    "subscription_id", "subscriptionId", "expires_at", "period", "type",
    "trb_user_id", "telegram_user_id", "user_id", "channel_id", "payment_id",
})


class InvalidTributeEvent(ValueError):
    pass


def verify_signature(body: bytes, signature: str | None, api_key: str | None) -> bool:
    """Compare HMAC of exact raw request bytes, never reformatted JSON."""
    if not api_key or not signature or not isinstance(body, bytes):
        return False
    expected = hmac.new(api_key.encode("utf-8"), body, hashlib.sha256).digest()
    received = signature.strip()
    if received.startswith("sha256="):
        received = received[7:]
    hex_valid = hmac.compare_digest(received.lower(), expected.hex())
    base64_valid = hmac.compare_digest(received, base64.b64encode(expected).decode("ascii"))
    return hex_valid or base64_valid


def parse_event(body: bytes) -> dict:
    if not body or len(body) > MAX_BODY_BYTES:
        raise InvalidTributeEvent("Invalid request size")
    try:
        event = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidTributeEvent("Malformed JSON") from exc
    if not isinstance(event, dict):
        raise InvalidTributeEvent("Expected an object")
    if not isinstance(event.get("name"), str) or not event["name"] or len(event["name"]) > 80:
        raise InvalidTributeEvent("Missing event name")
    if not isinstance(event.get("payload"), dict):
        raise InvalidTributeEvent("Missing event payload")
    created = event.get("created_at")
    if not isinstance(created, str) or len(created) > 64:
        raise InvalidTributeEvent("Missing event timestamp")
    try:
        timestamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("Timezone required")
    except ValueError as exc:
        raise InvalidTributeEvent("Invalid event timestamp") from exc
    return event


def parse_signed_delivery(body: bytes) -> dict:
    """Recognize signed connection probes without treating them as subscriptions.

    Normal webhook envelopes retain strict validation. A signed JSON object that
    is not a valid subscription envelope can be logged as a diagnostic event.
    A malformed recognized subscription MUST still be rejected.
    """
    try:
        return parse_event(body)
    except InvalidTributeEvent:
        if not body or len(body) > MAX_BODY_BYTES:
            raise
        try:
            obj = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise InvalidTributeEvent("Malformed JSON") from None
        if not isinstance(obj, dict):
            raise InvalidTributeEvent("Expected an object")
        if obj.get("name") in SUBSCRIPTION_EVENTS:
            raise InvalidTributeEvent("Malformed subscription webhook")
        name = obj.get("name")
        if not isinstance(name, str) or not name or len(name) > 80:
            name = "tribute_connection_test"
        payload = obj.get("payload")
        payload_fields = sorted(str(k)[:80] for k in payload)[:60] if isinstance(payload, dict) else []
        return {
            "name": name,
            "created_at": "1970-01-01T00:00:00Z",
            "sent_at": "",
            "payload": {"probe_fingerprint": hashlib.sha256(body).hexdigest()},
            "_envelope_fields": sorted(str(k)[:80] for k in obj)[:60],
            "_payload_fields": payload_fields,
        }


def event_key(event: dict) -> str:
    """Stable across Tribute retries if only sent_at changes."""
    stable = {k: event[k] for k in ("name", "created_at", "payload")}
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def safe_event_record(event: dict) -> dict:
    """Only explicitly allowed payment identifiers; no names, emails, or full payload."""
    payload = event["payload"]
    details = {}
    for key in SAFE_FIELDS:
        val = payload.get(key)
        if type(val) in (str, int) and len(str(val)) <= 128:
            details[key] = val
    return {
        "name": event["name"],
        "created_at": event["created_at"],
        "sent_at": str(event.get("sent_at", ""))[:64],
        "payload_fields": event.get("_payload_fields", sorted(str(key)[:80] for key in payload)[:60]),
        "envelope_fields": event.get("_envelope_fields", []),
        "details": details,
    }


async def store_event(event: dict) -> bool:
    """Durably enqueue for review; never record a payment or grant access."""
    from sqlalchemy import text
    from db.database import SessionLocal

    record = safe_event_record(event)
    state = "pending_review" if event["name"] in SUBSCRIPTION_EVENTS else "ignored"
    async with SessionLocal() as session:
        async with session.begin():
            inserted = (await session.execute(text("""
                INSERT INTO payment_webhook_events
                  (provider, event_key, signature_valid, status, payload)
                VALUES ('tribute', :event_key, TRUE, :status, CAST(:payload AS JSONB))
                ON CONFLICT (provider, event_key) DO NOTHING
                RETURNING id
            """), {
                "event_key": event_key(event),
                "status": state,
                "payload": json.dumps(record, ensure_ascii=False),
            })).scalar_one_or_none()
    return inserted is not None


async def recent_events(limit: int = 50) -> list[dict]:
    """Read-only admin journal."""
    from sqlalchemy import text
    from db.database import SessionLocal

    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id, event_key, status, payload, received_at
            FROM payment_webhook_events
            WHERE provider='tribute'
            ORDER BY received_at DESC, id DESC
            LIMIT :limit
        """), {"limit": min(max(limit, 1), 100)})).mappings().all()
    return [dict(row) for row in rows]
