"""Conservative Telegram group integration.

Admin config + diagnostics + explicit invitations for users with a community grant.
No removals, no automatic granting from payment links.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_group_access():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS telegram_group_settings (
                id INTEGER PRIMARY KEY CHECK (id=1),
                chat_id BIGINT,
                chat_title TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            INSERT INTO telegram_group_settings(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS telegram_group_invites (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                chat_id BIGINT NOT NULL,
                invite_link TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL DEFAULT 'issued',
                created_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            ALTER TABLE telegram_group_invites
            ADD COLUMN IF NOT EXISTS hidden_at TIMESTAMPTZ
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS group_removal_tests (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                chat_id BIGINT NOT NULL,
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                status TEXT NOT NULL DEFAULT 'armed',
                last_error TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                removed_at TIMESTAMPTZ
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_group_invite_owner
            ON telegram_group_invites(telegram_id, created_at DESC)
        """))

async def group_settings():
    async with SessionLocal() as session:
        row=(await session.execute(text("""
            SELECT chat_id, chat_title, updated_at
            FROM telegram_group_settings WHERE id=1
        """))).mappings().one()
        return dict(row)

async def save_group(chat_id, title, actor):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE telegram_group_settings
                SET chat_id=:chat_id, chat_title=:title, updated_at=NOW()
                WHERE id=1
            """), {"chat_id":chat_id,"title":title[:200]})
            await session.execute(text("""
                INSERT INTO subscription_audit_log(action, actor, details)
                VALUES ('group_configured', :actor,
                    jsonb_build_object('chat_id', CAST(:chat_id AS BIGINT)))
            """), {"actor":actor,"chat_id":chat_id})

async def has_group_access(telegram_id):
    """A manual entitlement or a verified, active paid subscription."""
    async with SessionLocal() as session:
        return (await session.execute(text("""
            SELECT
            EXISTS (
                SELECT 1 FROM access_grants
                WHERE telegram_id=:uid AND resource='community'
                  AND revoked_at IS NULL AND starts_at<=NOW()
                  AND (ends_at IS NULL OR ends_at>NOW())
            )
            OR EXISTS (
                SELECT 1 FROM subscriptions
                WHERE telegram_id=:uid AND status='active'
                  AND paid_until>NOW()
            )
        """),{"uid":telegram_id})).scalar_one()

async def record_invite(telegram_id,chat_id,invite_link,expires_at,actor):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                INSERT INTO telegram_group_invites
                    (telegram_id, chat_id, invite_link, expires_at, created_by)
                VALUES (:uid, :chat_id, :link, :expires_at, :actor)
            """), {"uid":telegram_id,"chat_id":chat_id,"link":invite_link,
                    "expires_at":expires_at,"actor":actor})
            await session.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'group_invitation_sent',:actor,
                  jsonb_build_object('chat_id',CAST(:chat_id AS BIGINT)))
            """), {"uid":telegram_id,"actor":actor,"chat_id":chat_id})

async def invite_for_request(telegram_id,chat_id,invite_link):
    """Check that a request came from exactly the person the invite was created for."""
    async with SessionLocal() as session:
        match=(await session.execute(text("""
            SELECT 1 FROM telegram_group_invites
            WHERE telegram_id=:uid AND chat_id=:chat_id
              AND invite_link=:link AND status='issued'
              AND expires_at>NOW()
        """),{"uid":telegram_id,"chat_id":chat_id,"link":invite_link})).first()
        return bool(match)

async def mark_joined(telegram_id,chat_id,invite_link):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE telegram_group_invites SET status='joined',completed_at=NOW()
                WHERE telegram_id=:uid AND chat_id=:chat_id
                  AND invite_link=:link AND status='issued'
            """),{"uid":telegram_id,"chat_id":chat_id,"link":invite_link})
            await session.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'group_join_approved','bot',
                  jsonb_build_object('chat_id',CAST(:chat_id AS BIGINT)))
            """),{"uid":telegram_id,"chat_id":chat_id})

async def latest_invites(telegram_id):
    async with SessionLocal() as session:
        records=(await session.execute(text("""
            SELECT chat_id,status,created_at,expires_at,completed_at
            FROM telegram_group_invites WHERE telegram_id=:uid
              AND hidden_at IS NULL
            ORDER BY id DESC LIMIT 30
        """),{"uid":telegram_id})).mappings()
        return [dict(x) for x in records]


async def group_removal_test(telegram_id):
    async with SessionLocal() as session:
        row=(await session.execute(text("""
            SELECT chat_id,enabled,status,last_error,updated_at,removed_at
            FROM group_removal_tests WHERE telegram_id=:uid
        """),{"uid":telegram_id})).mappings().first()
        return dict(row) if row else None


async def set_group_removal_test(telegram_id, chat_id, enabled, actor):
    async with SessionLocal() as session:
        async with session.begin():
            if enabled:
                await session.execute(text("""
                  INSERT INTO group_removal_tests(telegram_id,chat_id,enabled,status,last_error,updated_at,removed_at)
                  VALUES (:uid,:chat,TRUE,'armed',NULL,NOW(),NULL)
                  ON CONFLICT(telegram_id) DO UPDATE SET chat_id=EXCLUDED.chat_id,
                    enabled=TRUE,status='armed',last_error=NULL,updated_at=NOW(),removed_at=NULL
                """),{"uid":telegram_id,"chat":chat_id})
            else:
                await session.execute(text("""
                  UPDATE group_removal_tests
                  SET enabled=FALSE,status='disabled',updated_at=NOW()
                  WHERE telegram_id=:uid
                """),{"uid":telegram_id})
            await session.execute(text("""
              INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
              VALUES (:uid,'group_removal_test_toggled',:actor,
                jsonb_build_object('enabled',CAST(:enabled AS BOOLEAN),
                                   'chat_id',CAST(:chat AS BIGINT)))
            """),{"uid":telegram_id,"actor":actor,"enabled":enabled,"chat":chat_id})


async def eligible_removal_tests(grace_hours=24, limit=20):
    """Explicit opt-in tests obey the SAME grace period as global removals."""
    if grace_hours not in (0, 24, 72):
        raise ValueError("Invalid grace period")
    async with SessionLocal() as session:
        rows=(await session.execute(text("""
          SELECT t.telegram_id,t.chat_id FROM group_removal_tests t
          WHERE t.enabled=TRUE AND t.status IN ('armed','error')
            AND EXISTS (
              SELECT 1 FROM access_grants g
              WHERE g.telegram_id=t.telegram_id AND g.resource='community'
                AND g.revoked_at IS NULL AND g.starts_at<=NOW() AND g.ends_at IS NOT NULL
              UNION ALL
              SELECT 1 FROM subscriptions s
              WHERE s.telegram_id=t.telegram_id AND s.status='active'
                AND s.paid_until IS NOT NULL
            )
            AND NOT EXISTS (
              SELECT 1 FROM access_grants g WHERE g.telegram_id=t.telegram_id
                AND g.resource='community' AND g.revoked_at IS NULL
                AND g.starts_at>NOW()
            )
            AND NOT EXISTS (
              SELECT 1 FROM access_grants g WHERE g.telegram_id=t.telegram_id
              AND g.resource='community' AND g.revoked_at IS NULL
              AND g.starts_at<=NOW() AND (g.ends_at IS NULL OR g.ends_at>NOW())
            )
            AND NOT EXISTS (
              SELECT 1 FROM subscriptions s WHERE s.telegram_id=t.telegram_id
              AND s.status='active'
              AND (s.paid_until IS NULL OR s.paid_until>NOW())
            )
            AND (
              SELECT MAX(expires) FROM (
                SELECT g.ends_at AS expires FROM access_grants g
                WHERE g.telegram_id=t.telegram_id AND g.resource='community'
                  AND g.revoked_at IS NULL AND g.starts_at<=NOW() AND g.ends_at IS NOT NULL
                UNION ALL
                SELECT s.paid_until AS expires FROM subscriptions s
                WHERE s.telegram_id=t.telegram_id AND s.status='active'
                  AND s.paid_until IS NOT NULL
              ) dates
            ) + (CAST(:grace AS INTEGER)*INTERVAL '1 hour')<=NOW()
          ORDER BY t.updated_at ASC LIMIT :limit
        """),{"grace":grace_hours,"limit":limit})).mappings().all()
        return [dict(r) for r in rows]


async def finish_group_removal(telegram_id, status, error=None):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE group_removal_tests
                SET status=:status,last_error=:error,updated_at=NOW(),
                    removed_at=CASE WHEN :status='removed' THEN NOW() ELSE removed_at END
                WHERE telegram_id=:uid AND enabled=TRUE
            """),{"uid":telegram_id,"status":status,"error":error})
            if status=='removed':
                await session.execute(text("""
                  INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                  VALUES (:uid,'test_group_removal','bot','{}'::jsonb)
                """),{"uid":telegram_id})


async def rearm_group_removal_test(telegram_id):
    """Reset a previous successful test after access has been renewed.

    Opt-in is never enabled here: only existing enabled test records are reset.
    """
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE group_removal_tests
                SET status='armed', last_error=NULL, removed_at=NULL, updated_at=NOW()
                WHERE telegram_id=:uid AND enabled=TRUE
                  AND status IN ('removed','already_out','skipped_admin','unban_failed')
            """), {"uid":telegram_id})


async def rearm_group_removal_on_join(telegram_id, chat_id):
    """Rearm an opted-in removal test after a user rejoins the test group."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE group_removal_tests
                SET status='armed', last_error=NULL, removed_at=NULL, updated_at=NOW()
                WHERE telegram_id=:uid AND chat_id=:chat AND enabled=TRUE
                  AND status IN ('removed','already_out')
                  AND EXISTS (
                    SELECT 1 FROM access_grants g WHERE g.telegram_id=:uid
                      AND g.resource='community' AND g.revoked_at IS NULL
                      AND g.starts_at<=NOW()
                      AND (g.ends_at IS NULL OR g.ends_at>NOW())
                  )
            """), {"uid":telegram_id,"chat":chat_id})


async def rearm_renewed_removal_tests():
    """Repair previously completed tests whose accounts received renewed access."""
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE group_removal_tests AS t
                SET status='armed',last_error=NULL,removed_at=NULL,updated_at=NOW()
                WHERE t.enabled=TRUE AND t.status IN ('removed','already_out')
                  AND (
                    EXISTS (
                      SELECT 1 FROM access_grants g
                      WHERE g.telegram_id=t.telegram_id AND g.resource='community'
                        AND g.revoked_at IS NULL AND g.starts_at<=NOW()
                        AND (g.ends_at IS NULL OR g.ends_at>NOW())
                    )
                    OR EXISTS (
                      SELECT 1 FROM subscriptions s
                      WHERE s.telegram_id=t.telegram_id AND s.status='active'
                        AND s.paid_until>NOW()
                    )
                  )
            """))


async def issued_invites_for_user(telegram_id,chat_id):
    async with SessionLocal() as session:
        rows=(await session.execute(text("""
            SELECT id,invite_link FROM telegram_group_invites
            WHERE telegram_id=:uid AND chat_id=:chat
              AND status='issued' AND expires_at>NOW()
        """),{"uid":telegram_id,"chat":chat_id})).mappings().all()
        return [dict(x) for x in rows]


async def invalidate_issued_invites(telegram_id,chat_id):
    """Prevent old direct links from being treated as authorized."""
    async with SessionLocal() as session:
        async with session.begin():
            rows=(await session.execute(text("""
                UPDATE telegram_group_invites SET status='revoked'
                WHERE telegram_id=:uid AND chat_id=:chat AND status='issued'
                RETURNING invite_link
            """),{"uid":telegram_id,"chat":chat_id})).scalars().all()
            return list(rows)


async def direct_invite_owner(chat_id,invite_link):
    """Find exact owner and current validity, even after a link was revoked."""
    async with SessionLocal() as session:
        row=(await session.execute(text("""
            SELECT telegram_id,status,expires_at>NOW() AS in_time
            FROM telegram_group_invites
            WHERE chat_id=:chat AND invite_link=:link
            ORDER BY id DESC LIMIT 1
        """),{"chat":chat_id,"link":invite_link})).mappings().first()
        return dict(row) if row else None


async def clear_completed_invite_history(telegram_id, actor):
    """Hide only consumed, revoked or expired invites from this member's admin UI.

    This does not delete the invitation or modify an active Telegram link.
    """
    async with SessionLocal() as session:
        async with session.begin():
            if not (await session.execute(text(
                "SELECT 1 FROM users WHERE telegram_id=:uid"
            ),{"uid":telegram_id})).first():
                raise ValueError("Участник не найден")
            result = await session.execute(text("""
                UPDATE telegram_group_invites
                SET hidden_at=NOW()
                WHERE telegram_id=:uid AND hidden_at IS NULL
                  AND (status <> 'issued' OR expires_at <= NOW())
            """),{"uid":telegram_id})
            await session.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'group_invites_history_hidden',:actor,
                        jsonb_build_object('count',CAST(:count AS INTEGER)))
            """),{"uid":telegram_id,"actor":actor,"count":result.rowcount})
            return result.rowcount
