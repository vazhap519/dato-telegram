"""Read-only Mini App analytics derived from effective community entitlements.

Do not equate manual grants, payment receipts, and subscription state.
Amounts are kept separate by currency; no ungrounded conversions.
"""
from datetime import datetime, timezone
from sqlalchemy import text
from db.database import SessionLocal

ACCESS_CTE="""
WITH current_grants AS (
  SELECT telegram_id,
         BOOL_OR(ends_at IS NULL) AS lifetime,
         MAX(ends_at) FILTER(WHERE ends_at IS NOT NULL) AS grant_expiry
  FROM access_grants
  WHERE resource='community' AND revoked_at IS NULL AND starts_at<=NOW()
    AND (ends_at IS NULL OR ends_at>NOW())
  GROUP BY telegram_id
), current_subscriptions AS (
  SELECT telegram_id, MAX(paid_until) AS paid_expiry,COUNT(*) AS active_records
  FROM subscriptions
  WHERE status='active' AND paid_until>NOW()
  GROUP BY telegram_id
), paid_users AS (
  SELECT DISTINCT telegram_id FROM verified_payment_receipts
  WHERE payment_status='paid'
), effective AS (
  SELECT u.telegram_id,u.username,u.first_name,u.last_name,
    (g.telegram_id IS NOT NULL OR s.telegram_id IS NOT NULL) AS active,
    COALESCE(g.lifetime,FALSE) AS lifetime,
    s.telegram_id IS NOT NULL AS has_active_subscription,
    (s.telegram_id IS NOT NULL AND p.telegram_id IS NOT NULL) AS subscriber_with_verified_payment,
    CASE WHEN COALESCE(g.lifetime,FALSE) THEN NULL
         ELSE GREATEST(g.grant_expiry,s.paid_expiry) END AS expires_at,
    CASE WHEN g.telegram_id IS NOT NULL AND s.telegram_id IS NOT NULL THEN 'Ручной доступ и подписка'
         WHEN g.telegram_id IS NOT NULL THEN 'Ручной доступ'
         WHEN s.telegram_id IS NOT NULL THEN 'Подписка'
         ELSE 'Нет доступа' END AS basis
  FROM users u
  LEFT JOIN current_grants g ON g.telegram_id=u.telegram_id
  LEFT JOIN current_subscriptions s ON s.telegram_id=u.telegram_id
  LEFT JOIN paid_users p ON p.telegram_id=u.telegram_id
)
"""

TOTALS_SQL=ACCESS_CTE+"""
SELECT COUNT(*) AS registered,
 COUNT(*) FILTER(WHERE active) AS active,
 COUNT(*) FILTER(WHERE lifetime) AS lifetime,
 COUNT(*) FILTER(WHERE has_active_subscription) AS active_subscription_users,
 COUNT(*) FILTER(WHERE subscriber_with_verified_payment) AS active_subscribers_with_paid_receipt,
 COUNT(*) FILTER(WHERE active AND NOT lifetime AND expires_at>NOW()
                   AND expires_at<=NOW()+INTERVAL '7 days') AS expiring_7d,
 COUNT(*) FILTER(WHERE active AND NOT lifetime AND expires_at>NOW()
                   AND expires_at<=NOW()+INTERVAL '30 days') AS expiring_30d
FROM effective
"""
EXPIRY_SQL=ACCESS_CTE+"""
SELECT telegram_id,username,first_name,last_name,expires_at,basis
FROM effective
WHERE active AND NOT lifetime AND expires_at>NOW()
  AND expires_at<=NOW()+INTERVAL '7 days'
ORDER BY expires_at ASC,telegram_id ASC LIMIT 100
"""
PAYMENTS_SQL="""
SELECT COALESCE(currency,'Не указана') AS currency,
 COUNT(*) AS count,
 COALESCE(SUM(amount),0) AS amount,
 COUNT(*) FILTER(WHERE amount IS NULL) AS missing_amount
FROM verified_payment_receipts
WHERE payment_status='paid'
 AND (:period='all' OR verified_at >= NOW()-INTERVAL '30 days')
GROUP BY COALESCE(currency,'Не указана')
ORDER BY currency ASC
"""
PAYMENT_COUNTS_SQL="""
SELECT
 COUNT(*) FILTER(WHERE payment_status='paid') AS paid_events,
 COUNT(DISTINCT telegram_id) FILTER(WHERE payment_status='paid') AS buyers,
 COUNT(*) FILTER(WHERE payment_status IN ('refunded','chargeback')) AS reversals
FROM verified_payment_receipts
WHERE (:period='all' OR verified_at >= NOW()-INTERVAL '30 days')
"""

def iso(value):
    return value.isoformat() if isinstance(value,datetime) else None

async def report(period="30d"):
    if period not in ("30d","all"):
        raise ValueError("Неизвестный период")
    async with SessionLocal() as session:
        # A single read-only repeatable-read snapshot keeps totals, expiry list
        # and payment aggregates mutually consistent during webhook updates.
        async with session.begin():
            await session.execute(text("SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ"))
            total=dict((await session.execute(text(TOTALS_SQL))).mappings().one())
            due=(await session.execute(text(EXPIRY_SQL))).mappings().all()
            counts=dict((await session.execute(text(PAYMENT_COUNTS_SQL),{"period":period})).mappings().one())
            amounts=(await session.execute(text(PAYMENTS_SQL),{"period":period})).mappings().all()
    return {
        "as_of":datetime.now(timezone.utc).isoformat(),
        "period":period,
        "members":total,
        "expiring":[{
            "telegram_id":row["telegram_id"],"username":row["username"],
            "first_name":row["first_name"],"last_name":row["last_name"],
            "expires_at":iso(row["expires_at"]),"basis":row["basis"]
        } for row in due],
        "expiring_displayed":len(due),
        "expiring_limit":100,
        "payments":{
            "paid_events":counts["paid_events"],"buyers":counts["buyers"],
            "reversals":counts["reversals"],
            "by_currency":[{
                "currency":row["currency"],"count":row["count"],
                "amount":str(row["amount"]),"missing_amount":row["missing_amount"]
            } for row in amounts]
        },
        "definitions":{
            "active":"Уникальные пользователи с действующим ручным доступом или активной подпиской",
            "lifetime":"Действующий бессрочный ручной доступ",
            "active_subscription_users":"Уникальные пользователи с активной записью подписки",
            "active_subscribers_with_paid_receipt":"Активная подписка и хотя бы один подтверждённый платёж пользователя; платёж не обязательно относится к текущему периоду подписки",
            "expiring":"Дата окончания всего действующего доступа с учётом и ручных прав, и подписок; бессрочные доступы исключены",
            "payments":"Только подтверждённые paid записи; возвращённые и оспоренные платежи исключены. Суммы по валютам, без конвертации и без вычета комиссии",
        }
    }
