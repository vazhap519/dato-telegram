"""Owner-reviewed activation of a REAL, already signed Tribute test payment.

Deliberately narrow scope: only the verified $1/month offer 279302 / period 528940,
only new_subscription, only a user who claimed a private bot test link before paying.
No automatic revocations, no manipulation of existing manual/lifetime grants.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

from sqlalchemy import text

from db.tribute_payment_test import TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID

TEST_CHANNEL_ID = 547415
TEST_CENTS = 100
TEST_PLAN_CODE = "tribute_test_279302_monthly"
MAX_TELEGRAM_ID = 9223372036854775807
_TRIBUTE_ID_RE = re.compile(r"^T-[0-9]{1,18}$")


class TributeActivationError(ValueError):
    """Invalid or unrelated signed event: never grant group rights."""


def _int_exact(value: object) -> int | None:
    """Require a JSON integer, never boolean/string/float guessing."""
    return value if type(value) is int else None


def validate_test_purchase(record: dict, *, now: datetime | None = None) -> dict:
    """Fail closed: validate a sanitized record from the signed webhook inbox.

    This function by itself DOES NOT prove the signature. The caller MUST load a
    database row with signature_valid=TRUE and status=pending_review.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise TributeActivationError("Неверный часовой пояс проверки")
    if not isinstance(record, dict) or record.get("name") != "new_subscription":
        raise TributeActivationError("Это не первичная оплата подписки")
    details = record.get("details")
    if not isinstance(details, dict):
        raise TributeActivationError("Нет данных подтверждённого платежа")
    for field, expected in (
        ("subscription_id", TEST_SUBSCRIPTION_ID),
        ("period_id", TEST_PERIOD_ID),
        ("channel_id", TEST_CHANNEL_ID),
        ("amount", TEST_CENTS),
    ):
        if _int_exact(details.get(field)) != expected:
            raise TributeActivationError("Не соответствует тестовому тарифу: " + field)
    if details.get("currency") != "usd" or details.get("period") != "monthly":
        raise TributeActivationError("Неверная валюта или период подписки")
    if details.get("type") != "regular":
        raise TributeActivationError("Неверный тип подписки")

    uid = _int_exact(details.get("telegram_user_id"))
    if uid is None or not 0 < uid <= MAX_TELEGRAM_ID:
        raise TributeActivationError("Не указан Telegram ID покупателя")

    tribute_id = details.get("trb_user_id")
    if not isinstance(tribute_id, str) or not _TRIBUTE_ID_RE.fullmatch(tribute_id):
        raise TributeActivationError("Отсутствует корректный ID клиента Tribute")
    internal_id = _int_exact(details.get("user_id"))
    if internal_id is None or tribute_id != "T-" + str(internal_id):
        raise TributeActivationError("ID клиента Tribute противоречат друг другу")

    try:
        created_raw = record["created_at"]
        expires_raw = details["expires_at"]
        if not isinstance(created_raw, str) or not isinstance(expires_raw, str):
            raise ValueError("Invalid timestamp type")
        created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        if not created.tzinfo or not expires.tzinfo:
            raise ValueError("Timezone missing")
    except (KeyError, ValueError, TypeError) as exc:
        raise TributeActivationError("Срок подписки отсутствует или некорректен") from exc

    duration = expires - created
    if not timedelta(days=25) <= duration <= timedelta(days=35):
        raise TributeActivationError("Неправдоподобный срок месячной подписки")
    if expires <= now:
        raise TributeActivationError("Оплаченный период уже истёк")
    if created > now + timedelta(minutes=10):
        raise TributeActivationError("Событие подписки из будущего")
    return {
        "telegram_id": uid,
        "tribute_user_id": tribute_id,
        "paid_until": expires.astimezone(timezone.utc),
        "amount": Decimal(TEST_CENTS) / Decimal(100),
        "currency": "USD",
    }


async def initialize_tribute_test_activation() -> None:
    """Create buyer binding without modifying existing accounts or grants."""
    from db.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_test_buyer_bindings(
                offer_id BIGINT NOT NULL,
                trb_user_id TEXT NOT NULL,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                subscription_row_id BIGINT NOT NULL UNIQUE REFERENCES subscriptions(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(offer_id, trb_user_id)
            )
        """))


async def activate_saved_test_purchase(event_id: int, actor: str) -> dict:
    """Owner-approved, atomic activation of one signed/recorded live payment.

    Replays are idempotent; all changes commit together or none do.
    Invitations are sent by the *caller*, only after the transaction commits.
    """
    from db.database import SessionLocal

    if type(event_id) is not int or event_id <= 0:
        raise TributeActivationError("Неверный номер события")
    if not isinstance(actor, str) or not actor or len(actor) > 100:
        raise TributeActivationError("Отсутствует администратор")

    async with SessionLocal() as s:
        async with s.begin():
            row = (await s.execute(text("""
                SELECT id,event_key,signature_valid,status,payload,received_at
                FROM payment_webhook_events
                WHERE id=:id AND provider='tribute'
                FOR UPDATE
            """), {"id":event_id})).mappings().first()
            if not row or row["signature_valid"] is not True:
                raise TributeActivationError("Нет проверенного подписанного события")
            if row["status"] not in ("pending_review", "processed"):
                raise TributeActivationError("Это событие недоступно для активации")

            parsed = validate_test_purchase(row["payload"])
            uid = parsed["telegram_id"]
            buyer = parsed["tribute_user_id"]

            # A purchase seen in Tribute is not automatically proof that the
            # buyer came through *our* restricted bot checkout.
            signed_up = (await s.execute(text("""
                SELECT 1 FROM users WHERE telegram_id=:uid
            """), {"uid":uid})).first()
            if not signed_up:
                raise TributeActivationError("Покупатель ещё не запускал нашего бота")
            claim = (await s.execute(text("""
                SELECT 1 FROM tribute_payment_test_links
                WHERE claimed_by=:uid
                  AND claimed_at IS NOT NULL
                  AND claimed_at <= :received_at
                  AND claimed_at >= :received_at - INTERVAL '2 hours'
                LIMIT 1
            """), {"uid":uid, "received_at":row["received_at"]})).first()
            if not claim:
                raise TributeActivationError("Не найден тестовый вход покупателя через нашего бота")

            provider_ref = f"test:{TEST_SUBSCRIPTION_ID}:{buyer}"
            current = (await s.execute(text("""
                SELECT id, telegram_id, paid_until
                FROM subscriptions
                WHERE provider='tribute' AND provider_subscription_id=:ref
                FOR UPDATE
            """), {"ref":provider_ref})).mappings().first()

            if row["status"] == "processed":
                if not current or current["telegram_id"] != uid:
                    raise TributeActivationError("Обработанное событие не связано с покупателем")
                return {
                    **parsed, "subscription_row_id":current["id"],
                    "already_processed":True
                }

            # This plan is inactive and has no public payment URL: ordinary
            # customers cannot see the $1 test offer in the bot's tariff list.
            await s.execute(text("""
                INSERT INTO subscription_plans
                    (code,name,price_amount,currency,duration_days,is_lifetime,is_active,payment_url)
                VALUES
                    (:code,'Тестовый платёж Tribute · 1 USD/месяц',1.00,'USD',31,FALSE,FALSE,'')
                ON CONFLICT(code) DO NOTHING
            """), {"code": TEST_PLAN_CODE})
            plan = (await s.execute(text("""
                SELECT id FROM subscription_plans WHERE code=:code
            """), {"code": TEST_PLAN_CODE})).scalar_one()

            if current:
                if current["telegram_id"] != uid:
                    raise TributeActivationError("Платёж Tribute уже принадлежит другому Telegram ID")
                subscription_row_id = current["id"]
                await s.execute(text("""
                    UPDATE subscriptions
                    SET plan_id=:plan, status='active',
                        paid_until=GREATEST(paid_until,:expires),
                        renews_automatically=TRUE,updated_at=NOW()
                    WHERE id=:id AND telegram_id=:uid
                """), {"plan":plan, "expires":parsed["paid_until"],
                        "id":subscription_row_id, "uid":uid})
            else:
                subscription_row_id = (await s.execute(text("""
                    INSERT INTO subscriptions(
                        telegram_id,plan_id,status,provider,provider_subscription_id,
                        paid_until,renews_automatically
                    ) VALUES(:uid,:plan,'active','tribute',:ref,:expires,TRUE)
                    RETURNING id
                """), {"uid":uid,"plan":plan,"ref":provider_ref,
                        "expires":parsed["paid_until"]})).scalar_one()

            # Binding is immutable for the same Tribute identity. A second
            # Telegram account cannot claim this buyer in a later event.
            bound = (await s.execute(text("""
                SELECT telegram_id,subscription_row_id
                FROM tribute_test_buyer_bindings
                WHERE offer_id=:offer AND trb_user_id=:buyer
                FOR UPDATE
            """), {"offer":TEST_SUBSCRIPTION_ID,"buyer":buyer})).mappings().first()
            if bound and (bound["telegram_id"] != uid or
                          bound["subscription_row_id"] != subscription_row_id):
                raise TributeActivationError("Конфликт привязки покупателя Tribute")
            if not bound:
                await s.execute(text("""
                    INSERT INTO tribute_test_buyer_bindings
                        (offer_id,trb_user_id,telegram_id,subscription_row_id)
                    VALUES (:offer,:buyer,:uid,:sub)
                """), {"offer":TEST_SUBSCRIPTION_ID,"buyer":buyer,
                        "uid":uid,"sub":subscription_row_id})

            # Transaction ID was not provided by this webhook. The stable
            # signed event hash is used as an idempotent receipt key instead.
            await s.execute(text("""
                INSERT INTO verified_payment_receipts
                    (provider,provider_payment_id,telegram_id,subscription_id,
                     amount,currency,payment_status,source)
                VALUES ('tribute',:payment,:uid,:sub,:amount,:currency,'paid','verified_webhook')
                ON CONFLICT(provider,provider_payment_id) DO NOTHING
            """), {"payment":row["event_key"],"uid":uid,"sub":subscription_row_id,
                    "amount":parsed["amount"],"currency":parsed["currency"]})
            await s.execute(text("""
                UPDATE payment_webhook_events SET status='processed'
                WHERE id=:id AND status='pending_review'
            """), {"id":event_id})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'tribute_test_payment_activated',:actor,
                    jsonb_build_object('webhook_id',CAST(:eid AS BIGINT),
                                       'subscription_id',CAST(:sub AS BIGINT),
                                       'offer_id',CAST(:offer AS BIGINT)))
            """), {"uid":uid,"actor":actor,"eid":event_id,
                    "sub":subscription_row_id,"offer":TEST_SUBSCRIPTION_ID})
            return {
                **parsed,"subscription_row_id":subscription_row_id,
                "already_processed":False,
            }
