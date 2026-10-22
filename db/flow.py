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
            CREATE TABLE IF NOT EXISTS subscription_clicks (
                telegram_id BIGINT PRIMARY KEY,
                clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                click_count INTEGER NOT NULL DEFAULT 1
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
              (SELECT COUNT(DISTINCT telegram_id) FROM offer_acceptances) AS accepted,
              (SELECT COUNT(*) FROM subscription_clicks) AS subscription_clicked
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

async def record_subscription_click(telegram_id):
    """Record click intention only for someone who accepted the offer."""
    async with SessionLocal() as session:
        row = (await session.execute(text("""
            INSERT INTO subscription_clicks (telegram_id)
            SELECT :id
            WHERE EXISTS (SELECT 1 FROM offer_acceptances WHERE telegram_id=:id)
            ON CONFLICT (telegram_id) DO UPDATE SET
              click_count=subscription_clicks.click_count+1
            RETURNING telegram_id
        """), {"id": telegram_id})).first()
        await session.commit()
        return bool(row)

async def list_onboarding_users(stage="started", search="", page=1, per_page=30):
    if stage not in {"started", "accepted", "clicked"}:
        stage = "started"
    page = max(1, min(int(page), 100000))
    per_page = 30
    conditions = {
        "started": "TRUE",
        "accepted": "a.telegram_id IS NOT NULL",
        "clicked": "c.telegram_id IS NOT NULL"
    }
    query = """
        FROM users u
        LEFT JOIN (
            SELECT telegram_id, MIN(accepted_at) AS accepted_at
            FROM offer_acceptances GROUP BY telegram_id
        ) a ON a.telegram_id=u.telegram_id
        LEFT JOIN subscription_clicks c ON c.telegram_id=u.telegram_id
        WHERE """ + conditions[stage] + """
          AND (CAST(:search AS TEXT) = '' OR CAST(u.telegram_id AS TEXT) LIKE '%' || :search || '%')
    """
    params = {"search": search.strip()[:50], "limit": per_page, "offset": (page - 1) * per_page}
    async with SessionLocal() as session:
        total = (await session.execute(text("SELECT COUNT(*) " + query), params)).scalar_one()
        rows = (await session.execute(text("""
            SELECT u.telegram_id, u.created_at, a.accepted_at,
                   c.clicked_at, c.click_count
        """ + query + """
            ORDER BY u.created_at DESC, u.telegram_id DESC
            LIMIT :limit OFFSET :offset
        """), params)).mappings().all()
        return [dict(row) for row in rows], total
