"""Fail-closed, allowlisted Tribute subscription automation.

Only verified inbox rows are consumed. A disabled-by-default owner setting and
an exact offer/period/channel/price allowlist gate every grant. Cancellation
events are *never* treated as payment and this module NEVER removes members.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re

from sqlalchemy import text

from db.tribute_payment_test import TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID

TEST_CHANNEL_ID = 547415
TEST_PLAN_CODE = "tribute_test_279302_monthly"
CYCLES = {
    "weekly": (5, 9, 7),
    "monthly": (25, 35, 30),
    "quarterly": (85, 100, 90),
    "halfyearly": (170, 195, 180),
    "yearly": (350, 375, 365),
}
_TRIBUTE_USER = re.compile(r"^[A-Za-z0-9:_-]{1,100}$")
MAX_TG_ID = 9223372036854775807


class AutoPaymentError(ValueError):
    pass


def _exact_int(x):
    return x if type(x) is int else None


def _timestamp(x):
    if not isinstance(x, str) or len(x) > 64:
        raise AutoPaymentError("Недопустимая дата Tribute")
    try:
        dt = datetime.fromisoformat(x.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutoPaymentError("Неверный формат даты Tribute") from exc
    if dt.tzinfo is None:
        raise AutoPaymentError("Нет часового пояса Tribute")
    return dt.astimezone(timezone.utc)


def validate_paid_event(record, offer, now=None):
    """Validate original signed event's SANITIZED audit record (not an unsigned input).

    Require the caller to read a signature_valid=True inbox row inside a
    transaction. Only new/renewed regular payments with exact pricing qualify.
    No dependency on deprecated Tribute user_id.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise AutoPaymentError("Проверка времени требует часовой пояс")
    if not isinstance(record, dict) or record.get("name") not in (
        "new_subscription", "renewed_subscription"
    ):
        raise AutoPaymentError("Это не событие успешной оплаты")
    details = record.get("details")
    if not isinstance(details, dict):
        raise AutoPaymentError("Отсутствуют данные оплаты")
    for key, expected in (
        ("subscription_id", offer["subscription_id"]),
        ("period_id", offer["period_id"]),
        ("channel_id", offer["channel_id"]),
        ("amount", offer["amount_minor"]),
    ):
        if _exact_int(details.get(key)) != expected:
            raise AutoPaymentError("Неверный параметр оплаты: " + key)
    if details.get("currency") != offer["currency"] or details.get("period") != offer["period"]:
        raise AutoPaymentError("Несовпадение валюты или периода оплаты")
    if details.get("type") != "regular":
        raise AutoPaymentError("Платёж не относится к обычной платной подписке")

    uid = _exact_int(details.get("telegram_user_id"))
    buyer = details.get("trb_user_id")
    if uid is None or not 0 < uid <= MAX_TG_ID:
        raise AutoPaymentError("Нет корректного Telegram ID плательщика")
    if not isinstance(buyer, str) or not _TRIBUTE_USER.fullmatch(buyer):
        raise AutoPaymentError("Нет корректного Tribute ID плательщика")

    started = _timestamp(record.get("created_at"))
    ends = _timestamp(details.get("expires_at"))
    min_days, max_days, _ = CYCLES[offer["period"]]
    if not timedelta(days=min_days) <= ends - started <= timedelta(days=max_days):
        raise AutoPaymentError("Срок оплаты не соответствует выбранному периоду")
    if started > now + timedelta(minutes=10) or ends <= now:
        raise AutoPaymentError("Оплата ещё не наступила или период уже истёк")
    return {
        "event_name": record["name"], "telegram_id":uid, "trb_user_id":buyer,
        "expires_at":ends, "created_at":started,
        "price":Decimal(offer["amount_minor"]) / Decimal(100),
    }


async def initialize_tribute_auto():
    """Create independent auto-processing settings without enabling purchases."""
    from db.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_auto_settings (
                id INTEGER PRIMARY KEY CHECK (id=1),
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                enabled_at TIMESTAMPTZ,
                updated_by TEXT NOT NULL DEFAULT 'system',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            INSERT INTO tribute_auto_settings (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_auto_offers (
                subscription_id BIGINT NOT NULL,
                period_id BIGINT NOT NULL,
                channel_id BIGINT NOT NULL,
                amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                currency TEXT NOT NULL,
                period TEXT NOT NULL,
                plan_id BIGINT NOT NULL REFERENCES subscription_plans(id),
                require_test_claim BOOLEAN NOT NULL DEFAULT FALSE,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                enabled_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (subscription_id, period_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_auto_buyers (
                offer_id BIGINT NOT NULL,
                trb_user_id TEXT NOT NULL,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                subscription_row_id BIGINT NOT NULL UNIQUE REFERENCES subscriptions(id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (offer_id, trb_user_id)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_invite_outbox (
                event_id BIGINT PRIMARY KEY REFERENCES payment_webhook_events(id),
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                locked_until TIMESTAMPTZ,
                last_error TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS tribute_invite_outbox_due_idx
            ON tribute_invite_outbox (next_attempt_at)
        """))

        # The test price must never become a public tariff in the bot.
        await conn.execute(text("""
            INSERT INTO subscription_plans
                (code,name,price_amount,currency,duration_days,is_lifetime,is_active,payment_url)
            VALUES (:code,'Тестовый платёж Tribute · 1 USD/месяц',
                    1.00,'USD',31,FALSE,FALSE,'')
            ON CONFLICT(code) DO NOTHING
        """), {"code":TEST_PLAN_CODE})
        plan_id = (await conn.execute(text("""
            SELECT id FROM subscription_plans WHERE code=:code
        """), {"code":TEST_PLAN_CODE})).scalar_one()
        await conn.execute(text("""
            INSERT INTO tribute_auto_offers
                (subscription_id,period_id,channel_id,amount_minor,currency,
                 period,plan_id,require_test_claim,enabled)
            VALUES (:offer,:cycle,:chat,100,'usd','monthly',:plan,TRUE,FALSE)
            ON CONFLICT(subscription_id,period_id) DO NOTHING
        """), {"offer":TEST_SUBSCRIPTION_ID,"cycle":TEST_PERIOD_ID,
                "chat":TEST_CHANNEL_ID,"plan":plan_id})


async def list_auto_settings():
    from db.database import SessionLocal
    async with SessionLocal() as s:
        setting = (await s.execute(text("""
            SELECT enabled,enabled_at,updated_by,updated_at
            FROM tribute_auto_settings WHERE id=1
        """))).mappings().one()
        offers = (await s.execute(text("""
            SELECT o.*,p.name AS plan_name,p.is_lifetime,p.is_active AS plan_active,
                   p.currency AS plan_currency,p.price_amount AS plan_price,
                   p.duration_days AS plan_days
            FROM tribute_auto_offers o
            JOIN subscription_plans p ON p.id=o.plan_id
            ORDER BY o.subscription_id,o.period_id
        """))).mappings().all()
        plans = (await s.execute(text("""
            SELECT id,name,currency,price_amount,duration_days
            FROM subscription_plans
            WHERE archived_at IS NULL AND is_lifetime=FALSE
            ORDER BY id
        """))).mappings().all()
        backlog = (await s.execute(text("""
            SELECT count(*) FROM payment_webhook_events
            WHERE provider='tribute' AND status='pending_review'
        """))).scalar_one()
        outbox = (await s.execute(text("""
            SELECT state,count(*) qty FROM tribute_invite_outbox GROUP BY state
        """))).mappings().all()
    return {
        "setting":dict(setting), "offers":[dict(x) for x in offers],
        "plans":[dict(x) for x in plans], "pending_total":backlog,
        "outbox":{x["state"]:x["qty"] for x in outbox},
    }


async def register_auto_offer(offer_id, period_id, channel_id, amount_minor,
                              currency, period, plan_id, actor):
    """Allowlist exact offer and real bot plan; initially disabled."""
    from db.database import SessionLocal
    if any(type(x) is not int or x <= 0 for x in
           (offer_id,period_id,channel_id,amount_minor,plan_id)):
        raise AutoPaymentError("Укажите положительные числовые идентификаторы и сумму в центах")
    if currency not in ("usd","eur","gel") or period not in CYCLES:
        raise AutoPaymentError("Неверная валюта или период")
    if not isinstance(actor,str) or not 1 <= len(actor) <= 100:
        raise AutoPaymentError("Нет администратора")
    if offer_id == TEST_SUBSCRIPTION_ID:
        raise AutoPaymentError("Тестовый тариф настраивается отдельно")
    async with SessionLocal() as s:
        async with s.begin():
            plan = (await s.execute(text("""
                SELECT price_amount,currency,duration_days,is_lifetime,is_active,archived_at
                FROM subscription_plans WHERE id=:id FOR UPDATE
            """), {"id":plan_id})).mappings().first()
            if not plan or plan["archived_at"] or plan["is_lifetime"] or not plan["is_active"]:
                raise AutoPaymentError("Укажите активный месячный/годовой тариф без бессрочных прав")
            if plan["price_amount"] is None or plan["currency"].lower()!=currency:
                raise AutoPaymentError("Цена или валюта тарифа бота не совпадают с Tribute")
            if Decimal(plan["price_amount"]) != Decimal(amount_minor)/Decimal(100):
                raise AutoPaymentError("Стоимость тарифа не совпадает с Tribute")
            low,high,_ = CYCLES[period]
            if plan["duration_days"] is None or not low<=plan["duration_days"]<=high:
                raise AutoPaymentError("Срок тарифа бота не совпадает с Tribute")
            await s.execute(text("""
                INSERT INTO tribute_auto_offers
                    (subscription_id,period_id,channel_id,amount_minor,
                     currency,period,plan_id,enabled,require_test_claim)
                VALUES(:offer,:cycle,:chat,:amount,:currency,:period,:plan,FALSE,FALSE)
            """), {"offer":offer_id,"cycle":period_id,"chat":channel_id,
                    "amount":amount_minor,"currency":currency,
                    "period":period,"plan":plan_id})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES('tribute_auto_offer_added',:actor,
                    jsonb_build_object('offer',CAST(:offer AS BIGINT),
                                       'period_id',CAST(:cycle AS BIGINT)))
            """), {"actor":actor,"offer":offer_id,"cycle":period_id})


async def set_auto_offer_enabled(offer_id, period_id, enabled, actor):
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            offer = (await s.execute(text("""
                SELECT o.*,p.price_amount,p.currency AS plan_currency,
                       p.is_lifetime,p.is_active,p.archived_at,p.duration_days
                FROM tribute_auto_offers o JOIN subscription_plans p ON p.id=o.plan_id
                WHERE o.subscription_id=:offer AND o.period_id=:cycle FOR UPDATE
            """), {"offer":offer_id,"cycle":period_id})).mappings().first()
            if not offer:
                raise AutoPaymentError("Предложение Tribute не найдено")
            if enabled:
                if offer["is_lifetime"] or offer["archived_at"]:
                    raise AutoPaymentError("Бессрочный или архивный тариф запрещён")
                if not offer["require_test_claim"] and not offer["is_active"]:
                    raise AutoPaymentError("Публичный тариф бота должен быть активным")
                if offer["price_amount"] is None or (Decimal(offer["price_amount"])
                        != Decimal(offer["amount_minor"])/Decimal(100)
                        or offer["plan_currency"].lower()!=offer["currency"]):
                    raise AutoPaymentError("Цена тарифа не соответствует цене Tribute")
            await s.execute(text("""
                UPDATE tribute_auto_offers
                SET enabled=:enabled,
                    enabled_at=CASE WHEN :enabled THEN NOW() ELSE NULL END
                WHERE subscription_id=:offer AND period_id=:cycle
            """), {"enabled":enabled,"offer":offer_id,"cycle":period_id})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES ('tribute_auto_offer_toggled',:actor,
                    jsonb_build_object('offer',CAST(:offer AS BIGINT),
                                       'period_id',CAST(:cycle AS BIGINT),
                                       'enabled',CAST(:enabled AS BOOLEAN)))
            """), {"actor":actor,"offer":offer_id,"cycle":period_id,"enabled":enabled})


async def set_auto_processing_enabled(enabled, actor):
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            if enabled:
                count = (await s.execute(text("""
                    SELECT count(*) FROM tribute_auto_offers WHERE enabled=TRUE
                """))).scalar_one()
                if count == 0:
                    raise AutoPaymentError("Сначала включите хотя бы одно проверенное предложение")
            await s.execute(text("""
                UPDATE tribute_auto_settings
                SET enabled=:enabled,
                    enabled_at=CASE WHEN :enabled THEN NOW() ELSE NULL END,
                    updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """), {"enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES ('tribute_auto_toggled',:actor,
                    jsonb_build_object('enabled',CAST(:enabled AS BOOLEAN)))
            """), {"actor":actor,"enabled":enabled})
