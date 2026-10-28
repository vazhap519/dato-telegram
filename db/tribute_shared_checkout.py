"""One public Tribute checkout for four actual paid periods of Krug Dushi.

The link is presented only in our bot after accepting the current offer.
A click records intent for the *offer*, never an assumed purchase period.
All payment entitlements are determined by the signed webhook's periodId.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from sqlalchemy import text

PRODUCTION_OFFER_ID = 279348
# Verified from the actual USD $1 public-channel signed webhook; the new
# checkout screenshot shows the same public channel. Verify before enabling.
PUBLIC_CHANNEL_ID = 547415
PRODUCTION_URL = "https://t.me/tribute/app?startapp=s1aFC"
PRODUCTION_WEB_URL = "https://web.tribute.tg/s/1aFC"
PERIODS = (
    # Tribute period_id, period, price in cents, plan duration, display label.
    (529035, "monthly", 1900, 30, "1 месяц"),
    (529034, "quarterly", 4900, 90, "3 месяца"),
    (529033, "halfyearly", 9500, 180, "6 месяцев"),
    (529032, "yearly", 17900, 365, "1 год"),
)


class CheckoutNotReady(ValueError):
    pass


def expected_periods():
    return {period_id: {"period": cadence, "amount_minor": cents,
                        "days": days, "label": label}
            for period_id,cadence,cents,days,label in PERIODS}


async def initialize_shared_checkout():
    from db.database import engine
    async with engine.begin() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(638492701)"))
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_shared_checkout_settings(
                id INTEGER PRIMARY KEY CHECK (id=1),
                subscription_id BIGINT NOT NULL,
                checkout_url TEXT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                changed_by TEXT NOT NULL DEFAULT 'system',
                changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await db.execute(text("""
            INSERT INTO tribute_shared_checkout_settings
                (id, subscription_id, checkout_url)
            VALUES(1,:offer,:url) ON CONFLICT(id) DO NOTHING
        """), {"offer":PRODUCTION_OFFER_ID, "url":PRODUCTION_URL})
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_checkout_intents(
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                subscription_id BIGINT NOT NULL,
                offer_version TEXT NOT NULL,
                clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(telegram_id,subscription_id)
            )
        """))
        await db.execute(text("""
            CREATE INDEX IF NOT EXISTS tribute_checkout_intents_created_idx
              ON tribute_checkout_intents(clicked_at DESC)
        """))


def is_expected_production_offer(offer):
    """No other Tribute offers, prices or private group subscriptions qualify."""
    if not isinstance(offer, dict):
        return False
    expected = expected_periods().get(offer.get("period_id"))
    return bool(expected and
                type(offer.get("subscription_id")) is int and
                offer["subscription_id"] == PRODUCTION_OFFER_ID and
                type(offer.get("channel_id")) is int and
                offer["channel_id"] == PUBLIC_CHANNEL_ID and
                type(offer.get("amount_minor")) is int and
                offer["amount_minor"] == expected["amount_minor"] and
                offer.get("currency") == "eur" and
                offer.get("period") == expected["period"] and
                offer.get("require_test_claim") is False)


def production_offer_ready(offer, *, require_enabled=True):
    """Validate the stored exact offer AND the current bot payment plan.

    Call on every checkout attempt, even if an admin enabled it earlier.
    Never show a payment page for a drifted price, inactive tariff or URL.
    """
    if not is_expected_production_offer(offer):
        return False
    try:
        return bool(
            (offer["enabled"] or not require_enabled) and offer["is_active"] and
            not offer["is_lifetime"] and offer["archived_at"] is None and
            offer["plan_currency"]=="EUR" and
            offer["price_amount"] is not None and
            Decimal(offer["price_amount"])==Decimal(offer["amount_minor"])/100 and
            offer["payment_url"]==PRODUCTION_URL and
            offer["duration_days"]==expected_periods()[offer["period_id"]]["days"]
        )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


async def shared_checkout_status():
    """Read-only overview for the protected admin page."""
    from db.database import SessionLocal
    async with SessionLocal() as s:
        checkout = (await s.execute(text("""
            SELECT * FROM tribute_shared_checkout_settings WHERE id=1
        """))).mappings().one()
        offers = (await s.execute(text("""
            SELECT o.*,p.name,p.price_amount,p.currency AS plan_currency,
                   p.duration_days,p.is_active,p.archived_at,p.payment_url,p.is_lifetime
            FROM tribute_auto_offers o JOIN subscription_plans p ON p.id=o.plan_id
            WHERE o.subscription_id=:offer
        """), {"offer":PRODUCTION_OFFER_ID})).mappings().all()
    by_period={o["period_id"]:dict(o) for o in offers}
    checked=[]
    for period_id,cadence,cents,days,label in PERIODS:
        offer=by_period.get(period_id)
        valid=bool(offer and production_offer_ready(offer, require_enabled=False))
        checked.append({"period_id":period_id,"period":cadence,
                        "price_eur":Decimal(cents)/100,"label":label,
                        "configured":valid,"enabled":bool(offer and offer["enabled"])})
    return {"checkout":dict(checkout),"periods":checked,
            "all_ready":len(offers)==4 and all(x["configured"] and x["enabled"]
                                               for x in checked)}


async def set_shared_checkout_enabled(enabled, actor):
    from db.database import SessionLocal
    if type(enabled) is not bool or not isinstance(actor,str) or not 1<=len(actor)<=100:
        raise CheckoutNotReady("Неверные параметры администратора")
    async with SessionLocal() as s:
        async with s.begin():
            current=(await s.execute(text("""
                SELECT subscription_id, checkout_url
                FROM tribute_shared_checkout_settings WHERE id=1 FOR UPDATE
            """))).mappings().one()
            if enabled:
                if current["subscription_id"]!=PRODUCTION_OFFER_ID or current["checkout_url"]!=PRODUCTION_URL:
                    raise CheckoutNotReady("Неверная общая ссылка Tribute")
                global_on=(await s.execute(text("""
                    SELECT enabled FROM tribute_auto_settings WHERE id=1
                """))).scalar_one()
                if not global_on:
                    raise CheckoutNotReady("Сначала включите автоматическую обработку Tribute")
                records=(await s.execute(text("""
                    SELECT o.*,p.price_amount,p.currency AS plan_currency,
                           p.duration_days,p.is_active,p.archived_at,p.payment_url,p.is_lifetime
                    FROM tribute_auto_offers o JOIN subscription_plans p ON p.id=o.plan_id
                    WHERE o.subscription_id=:offer
                """), {"offer":PRODUCTION_OFFER_ID})).mappings().all()
                if len(records)!=4 or any(
                    not production_offer_ready(dict(o)) for o in records
                ):
                    raise CheckoutNotReady("Все четыре периода должны быть проверены и разрешены")
            await s.execute(text("""
                UPDATE tribute_shared_checkout_settings
                SET enabled=:enabled,changed_by=:actor,changed_at=NOW() WHERE id=1
            """), {"enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES('tribute_shared_checkout_toggled',:actor,
                       jsonb_build_object('enabled',CAST(:enabled AS BOOLEAN),
                                          'offer_id',CAST(:offer AS BIGINT)))
            """), {"actor":actor,"enabled":enabled,"offer":PRODUCTION_OFFER_ID})


async def begin_shared_checkout(telegram_id):
    """Return a single checkout URL after recording a recent buyer/offer intent.

    The intent proves only bot entry; a SIGNED, matching successful payment
    is still mandatory before granting any permissions.
    """
    from db.database import SessionLocal
    if type(telegram_id) is not int or telegram_id<=0:
        return None
    async with SessionLocal() as s:
        async with s.begin():
            data=(await s.execute(text("""
                SELECT c.subscription_id,c.checkout_url,c.enabled AS checkout_enabled,
                       a.enabled AS automation_enabled
                FROM tribute_shared_checkout_settings c
                JOIN tribute_auto_settings a ON a.id=1 WHERE c.id=1
            """))).mappings().one()
            if not (data["checkout_enabled"] and data["automation_enabled"]
                    and data["subscription_id"]==PRODUCTION_OFFER_ID
                    and data["checkout_url"]==PRODUCTION_URL):
                return None
            offers=(await s.execute(text("""
                SELECT o.*,p.price_amount,p.currency AS plan_currency,
                       p.duration_days,p.is_active,p.archived_at,
                       p.payment_url,p.is_lifetime
                FROM tribute_auto_offers o
                JOIN subscription_plans p ON p.id=o.plan_id
                WHERE o.subscription_id=:offer
            """), {"offer":PRODUCTION_OFFER_ID})).mappings().all()
            if len(offers)!=4 or any(
                not production_offer_ready(dict(offer)) for offer in offers
            ):
                return None
            accepted=(await s.execute(text("""
                SELECT f.offer_version
                FROM bot_flow_settings f
                JOIN offer_acceptances a
                  ON a.telegram_id=:uid AND a.offer_version=f.offer_version
                WHERE f.id=1
            """), {"uid":telegram_id})).scalar_one_or_none()
            if not accepted:
                return None
            await s.execute(text("""
                INSERT INTO tribute_checkout_intents
                    (telegram_id,subscription_id,offer_version,clicked_at)
                VALUES (:uid,:offer,:version,NOW())
                ON CONFLICT(telegram_id,subscription_id)
                DO UPDATE SET offer_version=EXCLUDED.offer_version,clicked_at=NOW()
            """), {"uid":telegram_id,"offer":PRODUCTION_OFFER_ID,
                    "version":accepted})
            return data["checkout_url"]
