"""Fail-closed, allowlisted Tribute subscription automation.

Only verified inbox rows are consumed. A disabled-by-default owner setting and
an exact offer/period/channel/price allowlist gate every grant. Cancellation
events are *never* treated as payment and this module NEVER removes members.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
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


def is_tribute_checkout(url):
    """Allow only real Tribute payment-link families as a production bot plan."""
    if not isinstance(url,str) or not url or len(url)>2000:
        return False
    try:
        parsed=urlsplit(url)
        if parsed.scheme!="https" or parsed.username or parsed.password:
            return False
        host=(parsed.hostname or "").lower()
        if host=="t.me" and parsed.path=="/tribute/app":
            values=parse_qs(parsed.query).get("startapp",[])
            return len(values)==1 and bool(re.fullmatch(r"s[A-Za-z0-9_-]{3,90}",values[0]))
        if host=="web.tribute.tg" and re.fullmatch(r"/s/[A-Za-z0-9_-]{3,90}",parsed.path):
            return not parsed.query
    except ValueError:
        return False
    return False


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
        # Both bot and admin services initialize the same tables on startup.
        # Serialize new DDL across processes to avoid first-deploy catalog races.
        await conn.execute(text("SELECT pg_advisory_xact_lock(638492701)"))
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
                SELECT price_amount,currency,duration_days,is_lifetime,is_active,archived_at,payment_url
                FROM subscription_plans WHERE id=:id FOR UPDATE
            """), {"id":plan_id})).mappings().first()
            if not plan or plan["archived_at"] or plan["is_lifetime"] or not plan["is_active"]:
                raise AutoPaymentError("Укажите активный месячный/годовой тариф без бессрочных прав")
            if not is_tribute_checkout(plan["payment_url"]):
                raise AutoPaymentError("В тарифе бота должна быть настоящая HTTPS-ссылка оплаты Tribute")
            if plan["price_amount"] is None or plan["currency"].lower()!=currency:
                raise AutoPaymentError("Цена или валюта тарифа бота не совпадают с Tribute")
            if Decimal(plan["price_amount"]) != Decimal(amount_minor)/Decimal(100):
                raise AutoPaymentError("Стоимость тарифа не совпадает с Tribute")
            low,high,_ = CYCLES[period]
            if plan["duration_days"] is None or not low<=plan["duration_days"]<=high:
                raise AutoPaymentError("Срок тарифа бота не совпадает с Tribute")
            inserted = (await s.execute(text("""
                INSERT INTO tribute_auto_offers
                    (subscription_id,period_id,channel_id,amount_minor,
                     currency,period,plan_id,enabled,require_test_claim)
                VALUES(:offer,:cycle,:chat,:amount,:currency,:period,:plan,FALSE,FALSE)
                ON CONFLICT(subscription_id,period_id) DO NOTHING
                RETURNING subscription_id
            """), {"offer":offer_id,"cycle":period_id,"chat":channel_id,
                    "amount":amount_minor,"currency":currency,
                    "period":period,"plan":plan_id})).scalar_one_or_none()
            if inserted is None:
                raise AutoPaymentError("Этот период Tribute уже добавлен")
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
                       p.is_lifetime,p.is_active,p.archived_at,p.duration_days,p.payment_url
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
                if not offer["require_test_claim"] and not is_tribute_checkout(offer["payment_url"]):
                    raise AutoPaymentError("У публичного тарифа бота должна быть ссылка Tribute")
                if offer["price_amount"] is None or (Decimal(offer["price_amount"])
                        != Decimal(offer["amount_minor"])/Decimal(100)
                        or offer["plan_currency"].lower()!=offer["currency"]):
                    raise AutoPaymentError("Цена тарифа не соответствует цене Tribute")
            await s.execute(text("""
                UPDATE tribute_auto_offers
                SET enabled=:enabled,
                    enabled_at=CASE WHEN :enabled THEN COALESCE(enabled_at,NOW()) ELSE NULL END
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
                    enabled_at=CASE WHEN :enabled THEN COALESCE(enabled_at,NOW()) ELSE NULL END,
                    updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """), {"enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES ('tribute_auto_toggled',:actor,
                    jsonb_build_object('enabled',CAST(:enabled AS BOOLEAN)))
            """), {"actor":actor,"enabled":enabled})


async def is_auto_processing_enabled():
    from db.database import SessionLocal
    async with SessionLocal() as s:
        row = (await s.execute(text("""
            SELECT enabled FROM tribute_auto_settings WHERE id=1
        """))).scalar_one_or_none()
        return row is True


async def pending_auto_event_ids(limit=20):
    """Only events received AFTER the offer/global owner opt-in qualify.

    Join on JSON text, never CAST untrusted webhook values to bigint in SQL.
    Values must still be type-checked in Python before any mutation.
    """
    from db.database import SessionLocal
    async with SessionLocal() as s:
        ids = (await s.execute(text("""
            SELECT e.id
            FROM payment_webhook_events e
            JOIN tribute_auto_offers o
              ON e.payload->'details'->>'subscription_id' = CAST(o.subscription_id AS TEXT)
             AND e.payload->'details'->>'period_id' = CAST(o.period_id AS TEXT)
            JOIN tribute_auto_settings a ON a.id=1
            WHERE e.provider='tribute' AND e.signature_valid=TRUE
              AND e.status='pending_review'
              AND e.payload->>'name' IN ('new_subscription','renewed_subscription')
              AND o.enabled=TRUE AND a.enabled=TRUE
              AND e.received_at >= o.enabled_at
              AND e.received_at >= a.enabled_at
            ORDER BY e.id ASC LIMIT :limit
        """), {"limit":min(max(int(limit),1),50)})).scalars().all()
    return list(ids)


async def mark_auto_event_attention(event_id, reason):
    """Only manually reviewable events; failures never remove access."""
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE payment_webhook_events SET status='needs_attention'
                WHERE id=:id AND provider='tribute' AND status='pending_review'
            """), {"id":event_id})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES ('tribute_auto_attention','tribute_worker',
                    jsonb_build_object('event_id',CAST(:event_id AS BIGINT),
                                       'reason',CAST(:reason AS TEXT)))
            """), {"event_id":event_id,"reason":str(reason)[:180]})


async def activate_auto_event(event_id):
    """Atomically record an allowlisted signed payment and enqueue invitation.

    Any bad payment/user association aborts the entire transaction.
    The bot worker calls this ONLY for rows selected by pending_auto_event_ids.
    """
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            event = (await s.execute(text("""
                SELECT id,event_key,signature_valid,status,payload,received_at
                FROM payment_webhook_events WHERE id=:id AND provider='tribute'
                FOR UPDATE
            """), {"id":event_id})).mappings().first()
            if not event or event["signature_valid"] is not True:
                raise AutoPaymentError("Событие Tribute не имеет проверенной подписи")
            if event["status"] != "pending_review":
                return "already_handled"

            data = event["payload"]
            details = data.get("details", {}) if isinstance(data,dict) else {}
            offer_id = _exact_int(details.get("subscription_id")) if isinstance(details,dict) else None
            period_id = _exact_int(details.get("period_id")) if isinstance(details,dict) else None
            if offer_id is None or period_id is None:
                raise AutoPaymentError("Отсутствуют числовые идентификаторы тарифа")
            setting = (await s.execute(text("""
                SELECT enabled,enabled_at FROM tribute_auto_settings WHERE id=1
                FOR SHARE
            """))).mappings().one()
            offer = (await s.execute(text("""
                SELECT * FROM tribute_auto_offers
                WHERE subscription_id=:offer AND period_id=:cycle FOR SHARE
            """), {"offer":offer_id,"cycle":period_id})).mappings().first()
            if not offer or not offer["enabled"] or not setting["enabled"]:
                return "disabled"
            if event["received_at"] < offer["enabled_at"] or event["received_at"] < setting["enabled_at"]:
                return "older_than_activation"
            parsed = validate_paid_event(data, offer)
            plan = (await s.execute(text("""
                SELECT id,price_amount,currency,is_lifetime,is_active,
                       archived_at,payment_url
                FROM subscription_plans WHERE id=:plan FOR SHARE
            """), {"plan":offer["plan_id"]})).mappings().first()
            if (not plan or plan["is_lifetime"] or plan["archived_at"]
                    or plan["price_amount"] is None
                    or Decimal(plan["price_amount"]) != parsed["price"]
                    or plan["currency"].lower() != offer["currency"]):
                raise AutoPaymentError("Тариф в боте больше не соответствует оплате Tribute")
            if not offer["require_test_claim"] and (
                not plan["is_active"] or not is_tribute_checkout(plan["payment_url"])
            ):
                raise AutoPaymentError("Реальный тариф бота выключен или ссылка оплаты изменена")
            uid, buyer = parsed["telegram_id"], parsed["trb_user_id"]

            # Serialize competing payments for the same customer and offer.
            provider_ref = (
                f"test:{offer_id}:{buyer}" if offer["require_test_claim"]
                else f"tribute:{offer_id}:{buyer}"
            )
            await s.execute(text("""
                SELECT pg_advisory_xact_lock(hashtext(:key))
            """), {"key":provider_ref})
            user = (await s.execute(text("""
                SELECT 1 FROM users WHERE telegram_id=:uid
            """), {"uid":uid})).first()
            if not user:
                raise AutoPaymentError("Покупатель не запускал нашего Telegram-бота")

            binding = (await s.execute(text("""
                SELECT telegram_id,subscription_row_id
                FROM tribute_auto_buyers
                WHERE offer_id=:offer AND trb_user_id=:buyer
                FOR UPDATE
            """), {"offer":offer_id,"buyer":buyer})).mappings().first()
            current = (await s.execute(text("""
                SELECT id,telegram_id,paid_until FROM subscriptions
                WHERE provider='tribute' AND provider_subscription_id=:ref
                FOR UPDATE
            """), {"ref":provider_ref})).mappings().first()

            # Reuse the already MANUALLY verified test payment if present.
            legacy = None
            if offer["require_test_claim"] and current and not binding:
                legacy = (await s.execute(text("""
                    SELECT telegram_id,subscription_row_id
                    FROM tribute_test_buyer_bindings
                    WHERE offer_id=:offer AND trb_user_id=:buyer
                """), {"offer":offer_id,"buyer":buyer})).mappings().first()
                if not legacy or legacy["telegram_id"] != uid or legacy["subscription_row_id"] != current["id"]:
                    raise AutoPaymentError("Нельзя переназначить ранее оплаченную тестовую подписку")

            if binding:
                if binding["telegram_id"] != uid:
                    raise AutoPaymentError("Tribute ID уже принадлежит другому Telegram-аккаунту")
                if not current or binding["subscription_row_id"] != current["id"]:
                    raise AutoPaymentError("Не найдена прежняя подписка плательщика")
            elif parsed["event_name"] == "renewed_subscription" and not legacy:
                raise AutoPaymentError("Продление без предыдущей подтверждённой подписки")
            elif parsed["event_name"] == "new_subscription":
                if offer["require_test_claim"]:
                    claimed = (await s.execute(text("""
                        SELECT 1 FROM tribute_payment_test_links
                        WHERE claimed_by=:uid AND claimed_at IS NOT NULL
                          AND claimed_at<=:received
                          AND claimed_at>=:earliest
                        LIMIT 1
                    """), {"uid":uid,"received":event["received_at"],
                            "earliest":event["received_at"]-timedelta(hours=2)})).first()
                    if not claimed:
                        raise AutoPaymentError("Покупатель не открыл тестовую ссылку нашего бота")
                else:
                    started = (await s.execute(text("""
                        SELECT 1 FROM offer_acceptances a
                        JOIN bot_flow_settings f ON f.id=1
                        WHERE a.telegram_id=:uid AND a.offer_version=f.offer_version
                        LIMIT 1
                    """), {"uid":uid})).first()
                    selected = (await s.execute(text("""
                        SELECT 1 FROM subscription_plan_selections
                        WHERE telegram_id=:uid AND plan_id=:plan
                          AND selected_at<=:when
                          AND selected_at>=:earliest
                        LIMIT 1
                    """), {"uid":uid,"plan":offer["plan_id"],
                            "when":event["received_at"],
                            "earliest":event["received_at"]-timedelta(hours=48)})).first()
                    if not started or not selected:
                        raise AutoPaymentError("Нет принятой оферты и выбора этого тарифа в боте")

            if current and current["telegram_id"] != uid:
                raise AutoPaymentError("Оплаченная подписка привязана к другому пользователю")
            if current and not binding and not legacy:
                raise AutoPaymentError("Существующая подписка не имеет проверенной связи с Tribute")

            # Existing paid_until is never reduced by stale/out-of-order events.
            if current:
                sub_id = current["id"]
                await s.execute(text("""
                    UPDATE subscriptions
                    SET paid_until=GREATEST(paid_until,:expiry),
                        plan_id=:plan,
                        status='active',renews_automatically=TRUE,
                        updated_at=NOW()
                    WHERE id=:id AND telegram_id=:uid
                """), {"id":sub_id,"uid":uid,"plan":offer["plan_id"],
                        "expiry":parsed["expires_at"]})
            else:
                sub_id = (await s.execute(text("""
                    INSERT INTO subscriptions (
                        telegram_id,plan_id,status,provider,provider_subscription_id,
                        paid_until,renews_automatically
                    ) VALUES (:uid,:plan,'active','tribute',:ref,:expiry,TRUE)
                    RETURNING id
                """), {"uid":uid,"plan":offer["plan_id"],"ref":provider_ref,
                        "expiry":parsed["expires_at"]})).scalar_one()
            if not binding:
                await s.execute(text("""
                    INSERT INTO tribute_auto_buyers
                        (offer_id,trb_user_id,telegram_id,subscription_row_id)
                    VALUES(:offer,:buyer,:uid,:sub)
                """), {"offer":offer_id,"buyer":buyer,"uid":uid,"sub":sub_id})

            result = (await s.execute(text("""
                INSERT INTO verified_payment_receipts (
                    provider,provider_payment_id,telegram_id,subscription_id,
                    amount,currency,payment_status,source
                ) VALUES ('tribute',:event_key,:uid,:sub,:price,:currency,
                          'paid','verified_webhook')
                ON CONFLICT(provider,provider_payment_id) DO NOTHING
                RETURNING id
            """), {"event_key":event["event_key"],"uid":uid,"sub":sub_id,
                    "price":parsed["price"],"currency":offer["currency"].upper()})).scalar_one_or_none()
            if result is None:
                receipt = (await s.execute(text("""
                    SELECT telegram_id,subscription_id
                    FROM verified_payment_receipts
                    WHERE provider='tribute' AND provider_payment_id=:key
                """), {"key":event["event_key"]})).mappings().first()
                if not receipt or receipt["telegram_id"] != uid or receipt["subscription_id"] != sub_id:
                    raise AutoPaymentError("Подтверждение платежа уже связано с другим участником")
            await s.execute(text("""
                INSERT INTO tribute_invite_outbox(event_id,telegram_id)
                VALUES (:id,:uid) ON CONFLICT(event_id) DO NOTHING
            """), {"id":event_id,"uid":uid})
            await s.execute(text("""
                UPDATE payment_webhook_events SET status='processed'
                WHERE id=:id AND status='pending_review'
            """), {"id":event_id})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'tribute_auto_paid','tribute_worker',
                   jsonb_build_object('webhook_id',CAST(:event AS BIGINT),
                                      'subscription_id',CAST(:sub AS BIGINT),
                                      'offer_id',CAST(:offer AS BIGINT),
                                      'kind',CAST(:kind AS TEXT)))
            """), {"uid":uid,"event":event_id,"sub":sub_id,
                    "offer":offer_id,"kind":parsed["event_name"]})
            return "processed"


async def claim_due_invites(limit=5):
    """Lease pending/expired invite attempts to a single worker process."""
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            rows = (await s.execute(text("""
                UPDATE tribute_invite_outbox o
                SET state='sending',attempts=attempts+1,
                    locked_until=NOW()+ INTERVAL '3 minutes',updated_at=NOW()
                WHERE o.event_id IN (
                    SELECT event_id FROM tribute_invite_outbox
                    WHERE ((state IN ('pending','retry') AND next_attempt_at<=NOW())
                        OR (state='sending' AND locked_until<NOW()))
                    ORDER BY next_attempt_at,event_id
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                )
                RETURNING event_id,telegram_id,attempts
            """), {"limit":min(max(int(limit),1),20)})).mappings().all()
    return [dict(r) for r in rows]


async def finish_invite(event_id, delivered, reason=""):
    from db.database import SessionLocal
    async with SessionLocal() as s:
        async with s.begin():
            # Leave failed delivery eligible for retry; do not undo paid access.
            await s.execute(text("""
                UPDATE tribute_invite_outbox
                SET state=:state,locked_until=NULL,
                    next_attempt_at=CASE WHEN :delivered THEN NOW()
                                         ELSE NOW()+ INTERVAL '30 minutes' END,
                    last_error=:reason,updated_at=NOW()
                WHERE event_id=:event_id AND state='sending'
            """), {"event_id":event_id,
                    "state":"sent" if delivered else "retry",
                    "delivered":delivered,
                    "reason":str(reason)[:180]})
