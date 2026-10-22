"""Subscription onboarding database operations."""
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_flow():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bot_flow_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                video_note_file_id TEXT NOT NULL DEFAULT '',
                offer_text TEXT NOT NULL DEFAULT '',
                offer_url TEXT NOT NULL DEFAULT '',
                offer_version TEXT NOT NULL DEFAULT '1',
                subscription_text TEXT NOT NULL DEFAULT '',
                subscription_url TEXT NOT NULL DEFAULT ''
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS offer_acceptances (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                offer_version TEXT NOT NULL,
                accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE(telegram_id, offer_version)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS video_note_upload (
                id INTEGER PRIMARY KEY CHECK (id=1),
                code_hash TEXT NOT NULL,
                expires_at TIMESTAMPTZ NOT NULL,
                telegram_id BIGINT
            )
        """))
        await conn.execute(text("""
            INSERT INTO bot_flow_settings (id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """))

async def get_flow_settings():
    async with SessionLocal() as session:
        row = (await session.execute(
            text("SELECT * FROM bot_flow_settings WHERE id=1")
        )).first()
        return dict(row._mapping) if row else None

async def save_flow_settings(values):
    async with SessionLocal() as session:
        await session.execute(text("""
            UPDATE bot_flow_settings SET
                video_note_file_id=:video_note_file_id,
                offer_text=:offer_text,
                offer_url=:offer_url,
                offer_version=:offer_version,
                subscription_text=:subscription_text,
                subscription_url=:subscription_url
            WHERE id=1
        """), values)
        await session.commit()

async def register_user(telegram_id):
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            INSERT INTO users (telegram_id) VALUES (:id)
            ON CONFLICT (telegram_id) DO NOTHING
            RETURNING id
        """), {"id": telegram_id})).scalar_one_or_none()
        await session.commit()
        return row is not None

async def accept_offer(telegram_id, version):
    async with SessionLocal() as session:
        await session.execute(text("""
            INSERT INTO offer_acceptances (telegram_id, offer_version)
            SELECT :id, :version
            WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id=:id)
            ON CONFLICT (telegram_id, offer_version) DO NOTHING
        """), {"id": telegram_id, "version": version})
        await session.commit()

async def registration_stats():
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            SELECT
              (SELECT COUNT(*) FROM users) AS total,
              (SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE) AS today,
              (SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days') AS week,
              (SELECT COUNT(DISTINCT telegram_id) FROM offer_acceptances) AS accepted
        """))).one()
        return dict(row._mapping)

async def create_upload_code(code):
    import hashlib
    async with SessionLocal() as session:
        await session.execute(text("""
            INSERT INTO video_note_upload (id, code_hash, expires_at, telegram_id)
            VALUES (1, :hash, NOW() + INTERVAL '15 minutes', NULL)
            ON CONFLICT (id) DO UPDATE SET
              code_hash=EXCLUDED.code_hash, expires_at=EXCLUDED.expires_at,
              telegram_id=NULL
        """), {"hash": hashlib.sha256(code.encode()).hexdigest()})
        await session.commit()

async def authorize_video_uploader(telegram_id, code):
    import hashlib
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            UPDATE video_note_upload
            SET telegram_id=:uid
            WHERE id=1 AND code_hash=:hash AND expires_at > NOW()
              AND telegram_id IS NULL
            RETURNING id
        """), {"uid": telegram_id, "hash": hashlib.sha256(code.encode()).hexdigest()})).first()
        await session.commit()
        return bool(row)

async def store_uploaded_video_note(telegram_id, file_id):
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            DELETE FROM video_note_upload
            WHERE id=1 AND telegram_id=:uid AND expires_at > NOW()
            RETURNING id
        """), {"uid": telegram_id})).first()
        if row:
            await session.execute(text("""
                UPDATE bot_flow_settings SET video_note_file_id=:fid WHERE id=1
            """), {"fid": file_id})
        await session.commit()
        return bool(row)
