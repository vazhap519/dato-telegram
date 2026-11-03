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
        events, _ = await event_journal(page=1, per_page=5)
        return dict(summary), events

EVENT_TYPES = {
    "all":"Все события",
    "registered":"Регистрации",
    "access":"Доступ и подписки",
    "payment":"Подтверждённые платежи",
    "support":"Поддержка",
    "broadcast":"Рассылки",
    "webhook":"Webhook Tribute",
}
EVENTS_SQL = """
    SELECT event_id,occurred_at,kind,telegram_id,description,actor,details FROM (
        SELECT u.id::text AS event_id,u.created_at occurred_at,'registered'::text kind,
               u.telegram_id,'Регистрация в боте'::text description,
               ''::text actor,''::text details
        FROM users u
        UNION ALL
        SELECT a.id::text,a.created_at,'access',a.telegram_id,
               CASE WHEN a.action='grant_access' THEN 'Доступ назначен'
                    WHEN a.action='revoke_grant' THEN 'Доступ отозван'
                    WHEN a.action='extend_community_access' THEN 'Доступ продлён'
                    WHEN a.action='change_grant_expiry' THEN 'Срок доступа изменён'
                    ELSE 'Изменение подписки' END,
               a.actor,a.action
        FROM subscription_audit_log a
        WHERE a.action IN ('grant_access','revoke_grant','extend_community_access',
                           'change_grant_expiry')
        UNION ALL
        SELECT v.id::text,v.verified_at,'payment',v.telegram_id,
               'Проверенная операция через ' || v.provider,
               v.provider, v.payment_status || ' · ' || v.provider_payment_id
        FROM verified_payment_receipts v
        UNION ALL
        SELECT t.id::text,t.created_at,'support',t.telegram_id,
               'Обращение в поддержку','',t.status
        FROM support_tickets t
        UNION ALL
        SELECT b.id::text,b.created_at,'broadcast',NULL::BIGINT,
               'Рассылка создана','',b.status
        FROM broadcasts b
        UNION ALL
        SELECT w.id::text,w.received_at,'webhook',NULL::BIGINT,
               'Событие Tribute: ' || w.status,w.provider,w.status
        FROM payment_webhook_events w
    ) all_events
"""

async def event_journal(search="",kind="all",date_from=None,date_to=None,page=1,per_page=30):
    """Server-side filtering; no HTML-rendered payloads or assumed successful payments."""
    from datetime import date
    if kind not in EVENT_TYPES:
        kind="all"
    search=(search or "").strip().lstrip("@")[:80]
    page=max(1,min(int(page),100000))
    per_page=max(1,min(int(per_page),100))
    for d in (date_from,date_to):
        if d is not None and not isinstance(d,date):
            raise ValueError("Неверный формат даты")
    if date_from and date_to and date_from>date_to:
        raise ValueError("Дата начала позже даты окончания")
    where="""
      WHERE (:kind='all' OR kind=:kind)
        AND (:search='' OR COALESCE(telegram_id::text,'') ILIKE '%'||:search||'%'
             OR description ILIKE '%'||:search||'%'
             OR actor ILIKE '%'||:search||'%')
        AND (CAST(:date_from AS DATE) IS NULL
             OR occurred_at >= CAST(:date_from AS DATE)::timestamp AT TIME ZONE 'Asia/Tbilisi')
        AND (CAST(:date_to AS DATE) IS NULL
             OR occurred_at < (CAST(:date_to AS DATE)+1)::timestamp AT TIME ZONE 'Asia/Tbilisi')
    """
    params={"kind":kind,"search":search,"date_from":date_from,"date_to":date_to,
            "limit":per_page,"offset":(page-1)*per_page}
    async with SessionLocal() as s:
        total=(await s.execute(text("SELECT COUNT(*) FROM ("+EVENTS_SQL+") events "+where),params)).scalar_one()
        rows=(await s.execute(text("""
            SELECT event_id,occurred_at,kind,telegram_id,description,actor,details
            FROM ("""+EVENTS_SQL+""") events
        """+where+""" ORDER BY occurred_at DESC,kind,event_id DESC
            LIMIT :limit OFFSET :offset
        """),params)).mappings().all()
        return [dict(x) for x in rows],total


async def initialize_health_snapshots():
    from db.database import engine
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS monitor_health_snapshots(
                id INTEGER PRIMARY KEY CHECK (id=1),
                checked_at TIMESTAMPTZ NOT NULL,
                details JSONB NOT NULL
            )
        """))

async def last_health_snapshot():
    from datetime import datetime,timezone
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT checked_at,details FROM monitor_health_snapshots WHERE id=1
        """))).mappings().first()
        if row is None:
            return {"state":"unknown","checked_at":None,"details":{}}
        age=(datetime.now(timezone.utc)-row["checked_at"]).total_seconds()
        return {
            "state":"stale" if age>45*60 else "fresh",
            "checked_at":row["checked_at"],
            "details":row["details"],
        }
