"""Grounded admin home metrics and recent events.

Distinct effective users are counted once, regardless of overlapping grants.
Confirmed buyers are counted only from verified payment receipts.
"""
from sqlalchemy import text
from db.database import SessionLocal

async def dashboard_data():
    async with SessionLocal() as s:
        summary=(await s.execute(text("""
          WITH rights AS (
            SELECT telegram_id,
                   BOOL_OR(ends_at IS NULL) AS permanent,
                   MAX(ends_at) FILTER (WHERE ends_at IS NOT NULL) AS latest_expiry
            FROM access_grants
            WHERE resource='community' AND revoked_at IS NULL AND starts_at<=NOW()
              AND (ends_at IS NULL OR ends_at>NOW())
            GROUP BY telegram_id
          ), effective AS (
            SELECT telegram_id, permanent, latest_expiry FROM rights
            UNION ALL
            SELECT telegram_id, FALSE, paid_until
            FROM subscriptions
            WHERE status='active' AND paid_until>NOW()
          ), users_rights AS (
            SELECT telegram_id,BOOL_OR(permanent) AS permanent,
                   MAX(latest_expiry) AS expiry
            FROM effective GROUP BY telegram_id
          )
          SELECT
            (SELECT COUNT(*) FROM users_rights) AS active_access,
            (SELECT COUNT(*) FROM users_rights WHERE permanent) AS lifetime,
            (SELECT COUNT(*) FROM users_rights
             WHERE NOT permanent AND expiry>NOW()
               AND expiry<=NOW()+INTERVAL '7 days') AS expiring_7d,
            (SELECT COUNT(DISTINCT telegram_id) FROM verified_payment_receipts
             WHERE payment_status='paid') AS verified_buyers,
            (SELECT COUNT(*) FROM verified_payment_receipts
             WHERE payment_status='paid') AS verified_payments,
            (SELECT COUNT(*) FROM support_tickets
             WHERE status IN ('open','pending')) AS support_waiting,
            (SELECT COUNT(*) FROM broadcasts
             WHERE status IN ('queued','sending')) AS broadcasts_pending
        """))).mappings().one()
        events=(await s.execute(text("""
            SELECT occurred_at,kind,telegram_id,description FROM (
              SELECT u.created_at occurred_at,'registered' kind,u.telegram_id,
                     'Регистрация в боте' description
              FROM users u
              UNION ALL
              SELECT a.created_at,'access',a.telegram_id,
                     CASE WHEN a.action='grant_access' THEN 'Доступ назначен'
                          WHEN a.action='revoke_grant' THEN 'Доступ отозван'
                          WHEN a.action='extend_community_access' THEN 'Доступ продлён'
                          ELSE 'Изменение подписки' END
              FROM subscription_audit_log a
              WHERE a.action IN ('grant_access','revoke_grant','extend_community_access')
              UNION ALL
              SELECT v.verified_at,'payment',v.telegram_id,
                     'Подтверждённая оплата: ' || v.provider
              FROM verified_payment_receipts v WHERE v.payment_status='paid'
              UNION ALL
              SELECT t.created_at,'support',t.telegram_id,'Обращение в поддержку'
              FROM support_tickets t
            ) event_log
            ORDER BY occurred_at DESC
            LIMIT 15
        """))).mappings().all()
        return dict(summary),[dict(row) for row in events]
