"""Admin-editable monitoring recipient. Never store bot tokens here."""
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_reliability_settings():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS reliability_alert_settings(
              id INTEGER PRIMARY KEY CHECK (id=1),
              telegram_chat_id BIGINT,
              enabled BOOLEAN NOT NULL DEFAULT FALSE,
              updated_by TEXT NOT NULL DEFAULT 'system',
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            INSERT INTO reliability_alert_settings(id) VALUES (1)
            ON CONFLICT(id) DO NOTHING
        """))

async def get_reliability_settings():
    async with SessionLocal() as s:
        result=(await s.execute(text("""
            SELECT telegram_chat_id,enabled,updated_by,updated_at
            FROM reliability_alert_settings WHERE id=1
        """))).mappings().one()
        return dict(result)

async def save_reliability_settings(chat_id, enabled, actor):
    if chat_id is not None and (chat_id <= 0 or chat_id > 9999999999999):
        raise ValueError("Введите корректный положительный Telegram ID личного чата")
    if enabled and chat_id is None:
        raise ValueError("Для уведомлений укажите Telegram ID")
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE reliability_alert_settings
                SET telegram_chat_id=:chat, enabled=:enabled,
                    updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """),{"chat":chat_id,"enabled":enabled,"actor":actor})
