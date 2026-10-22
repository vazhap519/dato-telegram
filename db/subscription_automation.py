"""Safe subscription automation settings and managed Telegram member registry.

Existing group members are NEVER assumed managed. A member is enrolled only when
the bot observes a join via one of its own direct invite links.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_subscription_automation():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS subscription_automation_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                removal_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                grace_hours INTEGER NOT NULL DEFAULT 24 CHECK(grace_hours IN (0,24,72)),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            INSERT INTO subscription_automation_settings(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS subscription_managed_members (
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                chat_id BIGINT NOT NULL,
                enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                removed_at TIMESTAMPTZ,
                PRIMARY KEY(telegram_id,chat_id)
            )
        """))

async def automation_settings():
    async with SessionLocal() as session:
        row=(await session.execute(text("""
            SELECT notifications_enabled,removal_enabled,grace_hours,updated_at
            FROM subscription_automation_settings WHERE id=1
        """))).mappings().one()
        return dict(row)

async def save_automation_settings(notifications_enabled,removal_enabled,grace_hours,actor):
    if grace_hours not in (0,24,72):
        raise ValueError("Допускается только 0, 24 или 72 часа")
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE subscription_automation_settings
                SET notifications_enabled=:notify,removal_enabled=:remove,
                    grace_hours=:grace,updated_at=NOW() WHERE id=1
            """),{"notify":notifications_enabled,"remove":removal_enabled,"grace":grace_hours})
            await session.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES ('automation_settings_changed',:actor,
                    jsonb_build_object('notifications',CAST(:notify AS BOOLEAN),
                                       'removal',CAST(:remove AS BOOLEAN),
                                       'grace_hours',CAST(:grace AS INTEGER)))
            """),{"actor":actor,"notify":notifications_enabled,
                  "remove":removal_enabled,"grace":grace_hours})

async def record_managed_join(telegram_id,chat_id,invite_link):
    """Enroll only on an actual group join via a still-recorded bot invite."""
    async with SessionLocal() as session:
        async with session.begin():
            record=(await session.execute(text("""
                SELECT id FROM telegram_group_invites
                WHERE telegram_id=:uid AND chat_id=:chat AND invite_link=:link
                ORDER BY id DESC LIMIT 1
            """),{"uid":telegram_id,"chat":chat_id,"link":invite_link})).first()
            if not record:
                return False
            await session.execute(text("""
                INSERT INTO subscription_managed_members
                    (telegram_id,chat_id,enrolled_at,last_joined_at,removed_at)
                VALUES (:uid,:chat,NOW(),NOW(),NULL)
                ON CONFLICT(telegram_id,chat_id) DO UPDATE
                SET last_joined_at=NOW(),removed_at=NULL
            """),{"uid":telegram_id,"chat":chat_id})
            await session.execute(text("""
                UPDATE telegram_group_invites
                SET status='joined',completed_at=NOW()
                WHERE id=:id
            """),{"id":record[0]})
            return True

async def mark_managed_removed(telegram_id,chat_id):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE subscription_managed_members SET removed_at=NOW()
                WHERE telegram_id=:uid AND chat_id=:chat
            """),{"uid":telegram_id,"chat":chat_id})

async def managed_count(chat_id):
    if not chat_id:
        return 0
    async with SessionLocal() as session:
        return (await session.execute(text("""
            SELECT count(*) FROM subscription_managed_members
            WHERE chat_id=:chat AND removed_at IS NULL
        """),{"chat":chat_id})).scalar_one()

async def eligible_managed_removals(chat_id,grace_hours,limit=20):
    """Only previously bot-enrolled users, and only with a known expired entitlement."""
    if not chat_id:
        return []
    async with SessionLocal() as session:
        rows=(await session.execute(text("""
            SELECT m.telegram_id,m.chat_id
            FROM subscription_managed_members m
            WHERE m.chat_id=:chat AND m.removed_at IS NULL
            AND (
                EXISTS (
                    SELECT 1 FROM access_grants g
                    WHERE g.telegram_id=m.telegram_id AND g.resource='community'
                      AND g.starts_at<=NOW()
                )
                OR EXISTS (
                    SELECT 1 FROM subscriptions s
                    WHERE s.telegram_id=m.telegram_id AND s.status='active'
                )
            )
            AND NOT EXISTS (
                SELECT 1 FROM access_grants g
                WHERE g.telegram_id=m.telegram_id AND g.resource='community'
                  AND g.revoked_at IS NULL AND g.starts_at<=NOW()
                  AND (g.ends_at IS NULL OR g.ends_at>NOW())
            )
            AND NOT EXISTS (
                SELECT 1 FROM subscriptions s
                WHERE s.telegram_id=m.telegram_id AND s.status='active'
                  AND (s.paid_until IS NULL OR s.paid_until>NOW())
            )
            AND COALESCE((
                SELECT MAX(expiry) FROM (
                    SELECT g.ends_at AS expiry
                    FROM access_grants g
                    WHERE g.telegram_id=m.telegram_id AND g.resource='community'
                      AND g.starts_at<=NOW() AND g.ends_at IS NOT NULL
                    UNION ALL
                    SELECT s.paid_until AS expiry
                    FROM subscriptions s
                    WHERE s.telegram_id=m.telegram_id AND s.status='active'
                      AND s.paid_until IS NOT NULL
                ) dates
            ),NOW()) + (CAST(:grace AS INTEGER)*INTERVAL '1 hour') <= NOW()
            ORDER BY m.last_joined_at ASC LIMIT :limit
        """),{"chat":chat_id,"grace":grace_hours,"limit":limit})).mappings().all()
        return [dict(row) for row in rows]
