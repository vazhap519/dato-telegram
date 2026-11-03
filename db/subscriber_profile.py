"""Admin CRM metadata for subscriber cards, independent from payment verification."""
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_subscriber_profiles():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS subscriber_admin_profiles(
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                note TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '',
                updated_by TEXT NOT NULL DEFAULT 'system',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))

async def get_subscriber_profile(telegram_id):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT note,tags,updated_by,updated_at
            FROM subscriber_admin_profiles WHERE telegram_id=:uid
        """),{"uid":telegram_id})).mappings().first()
        return dict(row) if row else {"note":"","tags":"","updated_by":None,"updated_at":None}

async def save_subscriber_profile(telegram_id,note,tags,actor):
    note=(note or "").strip()
    tags=(tags or "").strip()
    if len(note)>3000:
        raise ValueError("Заметка не должна превышать 3000 символов")
    import re
    labels=[x.strip() for x in re.split(r"[,\n]",tags) if x.strip()]
    if len(labels)>15 or any(len(x)>40 for x in labels):
        raise ValueError("Допустимо до 15 меток, каждая не длиннее 40 символов")
    tags=", ".join(dict.fromkeys(labels))
    async with SessionLocal() as s:
        async with s.begin():
            exists=(await s.execute(text("SELECT 1 FROM users WHERE telegram_id=:uid"),
                                    {"uid":telegram_id})).first()
            if not exists:
                raise ValueError("Участник не найден")
            await s.execute(text("""
                INSERT INTO subscriber_admin_profiles(telegram_id,note,tags,updated_by)
                VALUES (:uid,:note,:tags,:actor)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    note=EXCLUDED.note,tags=EXCLUDED.tags,
                    updated_by=EXCLUDED.updated_by,updated_at=NOW()
            """),{"uid":telegram_id,"note":note,"tags":tags,"actor":actor})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'subscriber_profile_updated',:actor,
                  jsonb_build_object('tags',CAST(:tags AS TEXT),'note_changed',TRUE))
            """),{"uid":telegram_id,"tags":tags,"actor":actor})

async def subscriber_verified_payments(telegram_id):
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT v.id,v.provider,p.display_name AS provider_name,
                   v.provider_payment_id,v.amount,v.currency,
                   v.payment_status,v.verified_at
            FROM verified_payment_receipts v
            LEFT JOIN payment_provider_settings p ON p.code=v.provider
            WHERE v.telegram_id=:uid ORDER BY v.verified_at DESC,v.id DESC LIMIT 100
        """),{"uid":telegram_id})).mappings().all()
        return [dict(row) for row in rows]

async def subscriber_access_summary(telegram_id):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT
             EXISTS(SELECT 1 FROM access_grants g
               WHERE g.telegram_id=:uid AND g.resource='community'
                 AND g.revoked_at IS NULL AND g.starts_at<=NOW()
                 AND g.ends_at IS NULL) AS permanent,
             EXISTS(SELECT 1 FROM access_grants g
               WHERE g.telegram_id=:uid AND g.resource='community'
                 AND g.revoked_at IS NULL AND g.starts_at<=NOW()
                 AND (g.ends_at IS NULL OR g.ends_at>NOW()))
              OR EXISTS(SELECT 1 FROM subscriptions sub
               WHERE sub.telegram_id=:uid AND sub.status='active'
                 AND (sub.paid_until IS NULL OR sub.paid_until>NOW())) AS active,
             (SELECT MAX(expiry) FROM (
                SELECT ends_at expiry FROM access_grants
                WHERE telegram_id=:uid AND resource='community'
                  AND revoked_at IS NULL AND starts_at<=NOW()
                UNION ALL
                SELECT paid_until expiry FROM subscriptions
                WHERE telegram_id=:uid AND status='active'
             ) t) AS latest_expiry,
             (SELECT count(*) FROM verified_payment_receipts
              WHERE telegram_id=:uid AND payment_status='paid') AS payment_count,
             (SELECT count(*) FROM access_grants
              WHERE telegram_id=:uid AND resource='community'
                AND revoked_at IS NULL AND starts_at<=NOW()
                AND (ends_at IS NULL OR ends_at>NOW())) AS active_grants
        """),{"uid":telegram_id})).mappings().one()
        return dict(row)
