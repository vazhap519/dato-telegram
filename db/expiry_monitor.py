"""Subscription expiry monitoring in observation mode.

Only existing admin grants and verified subscription records count as access.
No Telegram removal or payment assumptions. Notifications are deduplicated.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

ACCESS_CTE = """
WITH sources AS (
    SELECT telegram_id, ends_at AS expires_at
    FROM access_grants
    WHERE resource='community' AND revoked_at IS NULL AND starts_at<=NOW()
    UNION ALL
    SELECT telegram_id, paid_until AS expires_at
    FROM subscriptions
    WHERE status='active' AND paid_until IS NOT NULL
      AND paid_until>NOW()-INTERVAL '30 days'
),
effective AS (
    SELECT telegram_id,
           BOOL_OR(expires_at IS NULL) AS permanent,
           MAX(expires_at) AS last_expires
    FROM sources GROUP BY telegram_id
)
"""

async def initialize_expiry_monitor():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS subscription_expiry_notifications(
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                alert_kind TEXT NOT NULL CHECK(alert_kind IN ('three_days','one_day','expired')),
                expires_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(telegram_id, alert_kind, expires_at)
            )
        """))

async def expiry_overview(limit=50):
    """Show one effective expiration per user; permanent grants override finite grants."""
    async with SessionLocal() as s:
        data=(await s.execute(text(ACCESS_CTE + """
            SELECT u.telegram_id, u.username, u.first_name, e.last_expires,
                CASE
                  WHEN e.permanent THEN 'lifetime'
                  WHEN e.last_expires<=NOW() THEN 'expired'
                  WHEN e.last_expires<=NOW()+INTERVAL '3 days' THEN 'expiring'
                  ELSE 'active'
                END AS access_status
            FROM effective e JOIN users u USING (telegram_id)
            WHERE NOT e.permanent AND e.last_expires<=NOW()+INTERVAL '3 days'
              AND e.last_expires>NOW()-INTERVAL '30 days'
            ORDER BY e.last_expires ASC LIMIT :limit
        """),{"limit":limit})).mappings()
        return [dict(x) for x in data]

async def expiry_counts():
    async with SessionLocal() as s:
        result=(await s.execute(text(ACCESS_CTE+"""
            SELECT
            COUNT(*) FILTER (WHERE NOT permanent AND last_expires BETWEEN NOW() AND NOW()+INTERVAL '3 days') AS expiring,
            COUNT(*) FILTER (WHERE NOT permanent AND last_expires<=NOW()
                AND last_expires>NOW()-INTERVAL '30 days') AS expired,
            COUNT(*) FILTER (WHERE permanent) AS permanent
            FROM effective
        """))).mappings().one()
        return dict(result)

async def claim_expiry_alerts(limit=40):
    """Queue the appropriate reminder as its time window actually arrives.

    One-day and expired notices are independent; a short test still gets
    a distinct expiration notification. Previously sent notices stay deduped.
    """
    async with SessionLocal() as s:
        async with s.begin():
            records=(await s.execute(text(ACCESS_CTE+"""
                SELECT e.telegram_id,e.last_expires,alert_window.kind
                FROM effective e
                CROSS JOIN LATERAL (
                    SELECT CASE
                      WHEN e.last_expires<=NOW() THEN 'expired'
                      WHEN e.last_expires<=NOW()+INTERVAL '1 day' THEN 'one_day'
                      ELSE 'three_days'
                    END AS kind
                ) alert_window
                WHERE NOT e.permanent
                  AND e.last_expires IS NOT NULL
                  AND e.last_expires>NOW()-INTERVAL '1 day'
                  AND e.last_expires<=NOW()+INTERVAL '3 days'
                  AND NOT EXISTS (
                    SELECT 1 FROM subscription_expiry_notifications n
                    WHERE n.telegram_id=e.telegram_id
                      AND n.expires_at=e.last_expires
                      AND n.alert_kind=alert_window.kind
                      AND (
                        n.status IN ('sent','pending')
                        OR (n.status='failed'
                            AND n.attempted_at>NOW()-INTERVAL '5 minutes')
                      )
                  )
                ORDER BY e.last_expires ASC
                LIMIT :limit
            """),{"limit":limit})).mappings().all()
            alerts=[]
            for rec in records:
                inserted=(await s.execute(text("""
                    INSERT INTO subscription_expiry_notifications
                      (telegram_id,alert_kind,expires_at)
                    VALUES (:uid,:kind,:exp)
                    ON CONFLICT(telegram_id,alert_kind,expires_at)
                    DO UPDATE SET status='pending'
                    WHERE subscription_expiry_notifications.status='failed'
                      AND subscription_expiry_notifications.attempted_at
                          <NOW()-INTERVAL '5 minutes'
                    RETURNING id
                """),{"uid":rec["telegram_id"],"kind":rec["kind"],"exp":rec["last_expires"]})).scalar_one_or_none()
                if inserted is not None:
                    alerts.append(dict(id=inserted,telegram_id=rec["telegram_id"],
                                       alert_kind=rec["kind"],expires_at=rec["last_expires"]))
            return alerts

async def finish_expiry_alert(alert_id, success):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE subscription_expiry_notifications
                SET status=:status, attempted_at=NOW() WHERE id=:id
            """),{"id":alert_id,"status":"sent" if success else "failed"})


async def expiry_alert_still_valid(alert):
    """Revalidate pending alert immediately before delivery, including renewals."""
    async with SessionLocal() as s:
        match=(await s.execute(text(ACCESS_CTE+"""
            SELECT 1 FROM effective e
            WHERE e.telegram_id=:uid
              AND e.permanent=FALSE
              AND e.last_expires=:expires
              AND (
                (:kind='three_days' AND e.last_expires>NOW()+INTERVAL '1 day'
                  AND e.last_expires<=NOW()+INTERVAL '3 days')
                OR (:kind='one_day' AND e.last_expires>NOW()
                  AND e.last_expires<=NOW()+INTERVAL '1 day')
                OR (:kind='expired' AND e.last_expires<=NOW()
                  AND e.last_expires>NOW()-INTERVAL '1 day')
              )
              AND NOT EXISTS (
                SELECT 1 FROM access_grants g
                WHERE g.telegram_id=e.telegram_id AND g.resource='community'
                  AND g.revoked_at IS NULL AND g.starts_at>NOW()
              )
        """),{"uid":alert["telegram_id"],"expires":alert["expires_at"],
               "kind":alert["alert_kind"]})).first()
        return bool(match)


async def skip_expiry_alert(alert_id):
    """Remember that a queued notice was superseded, without claiming delivery."""
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE subscription_expiry_notifications
                SET status='skipped',attempted_at=NOW()
                WHERE id=:id AND status='pending'
            """),{"id":alert_id})
