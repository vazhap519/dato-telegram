"""Conservative moderation of Telegram service messages in one configured group.

No filtering of ordinary user content and no changes to subscription membership.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

FIELDS = ("join_enabled", "leave_enabled", "pin_enabled", "other_enabled")

async def initialize_group_moderation():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_settings (
                id INTEGER PRIMARY KEY CHECK (id=1),
                join_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                leave_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                pin_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                other_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                updated_by TEXT NOT NULL DEFAULT 'system',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            INSERT INTO group_moderation_settings(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS group_moderation_statistics (
                id INTEGER PRIMARY KEY CHECK (id=1),
                deleted_total BIGINT NOT NULL DEFAULT 0,
                failed_total BIGINT NOT NULL DEFAULT 0,
                last_deleted_at TIMESTAMPTZ
            )
        """))
        await c.execute(text("""
            INSERT INTO group_moderation_statistics(id) VALUES(1)
            ON CONFLICT(id) DO NOTHING
        """))

async def get_moderation_settings():
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT join_enabled,leave_enabled,pin_enabled,other_enabled,
                   updated_by,updated_at
            FROM group_moderation_settings WHERE id=1
        """))).mappings().one()
        return dict(row)

async def save_moderation_settings(join_enabled,leave_enabled,pin_enabled,
                                   other_enabled,actor):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE group_moderation_settings
                SET join_enabled=:joined,leave_enabled=:left,pin_enabled=:pinned,
                    other_enabled=:other,updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """),{"joined":join_enabled,"left":leave_enabled,
                  "pinned":pin_enabled,"other":other_enabled,"actor":actor})

async def moderation_stats():
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT deleted_total,failed_total,last_deleted_at
            FROM group_moderation_statistics WHERE id=1
        """))).mappings().one()
        return dict(row)

async def count_moderation_action(success):
    async with SessionLocal() as s:
        async with s.begin():
            if success:
                await s.execute(text("""
                    UPDATE group_moderation_statistics
                    SET deleted_total=deleted_total+1,last_deleted_at=NOW() WHERE id=1
                """))
            else:
                await s.execute(text("""
                    UPDATE group_moderation_statistics
                    SET failed_total=failed_total+1 WHERE id=1
                """))
