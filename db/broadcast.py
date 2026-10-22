"""Broadcast queue: one database-backed campaign at a time, per-recipient status.

No broadcasting runs inside the FastAPI web process.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

AUDIENCES = {"started", "accepted", "clicked"}

async def initialize_broadcasts():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS broadcast_opt_out (
                telegram_id BIGINT PRIMARY KEY,
                opted_out_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS broadcasts (
                id BIGSERIAL PRIMARY KEY,
                body TEXT NOT NULL,
                audience TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                CHECK (audience IN ('started','accepted','clicked'))
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS broadcast_recipients (
                broadcast_id BIGINT NOT NULL REFERENCES broadcasts(id),
                telegram_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_code TEXT,
                sent_at TIMESTAMPTZ,
                PRIMARY KEY (broadcast_id, telegram_id)
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_broadcast_recipients_status
            ON broadcast_recipients (broadcast_id, status)
        """))

async def opt_out(telegram_id):
    async with SessionLocal() as session:
        await session.execute(text("""
            INSERT INTO broadcast_opt_out (telegram_id) VALUES (:id)
            ON CONFLICT (telegram_id) DO NOTHING
        """), {"id": telegram_id})
        await session.commit()

async def opt_in(telegram_id):
    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM broadcast_opt_out WHERE telegram_id=:id"), {"id": telegram_id})
        await session.commit()

async def count_recipients(audience):
    if audience not in AUDIENCES:
        raise ValueError("Unknown audience")
    async with SessionLocal() as session:
        return (await session.execute(text("""
            SELECT COUNT(*) FROM users u
            WHERE NOT EXISTS (SELECT 1 FROM broadcast_opt_out o WHERE o.telegram_id=u.telegram_id)
              AND (:audience='started'
                OR (:audience='accepted' AND EXISTS (SELECT 1 FROM offer_acceptances a WHERE a.telegram_id=u.telegram_id))
                OR (:audience='clicked' AND EXISTS (SELECT 1 FROM subscription_clicks c WHERE c.telegram_id=u.telegram_id)))
        """), {"audience": audience})).scalar_one()

async def create_broadcast(body, audience, scheduled_at=None):
    if audience not in AUDIENCES or not (1 <= len(body.strip()) <= 4000):
        raise ValueError("Invalid broadcast")
    async with SessionLocal() as session:
        async with session.begin():
            campaign_id = (await session.execute(text("""
                INSERT INTO broadcasts (body, audience, scheduled_at) VALUES (:body, :audience, :scheduled_at)
                RETURNING id
            """), {"body": body.strip(), "audience": audience, "scheduled_at": scheduled_at})).scalar_one()
            await session.execute(text("""
                INSERT INTO broadcast_recipients (broadcast_id, telegram_id)
                SELECT :bid, u.telegram_id FROM users u
                WHERE NOT EXISTS (SELECT 1 FROM broadcast_opt_out o WHERE o.telegram_id=u.telegram_id)
                  AND (:audience='started'
                   OR (:audience='accepted' AND EXISTS (SELECT 1 FROM offer_acceptances a WHERE a.telegram_id=u.telegram_id))
                   OR (:audience='clicked' AND EXISTS (SELECT 1 FROM subscription_clicks c WHERE c.telegram_id=u.telegram_id)))
            """), {"bid": campaign_id, "audience": audience})
            await session.execute(text("""
                UPDATE broadcasts SET status='completed', completed_at=NOW()
                WHERE id=:id AND NOT EXISTS (
                    SELECT 1 FROM broadcast_recipients WHERE broadcast_id=:id
                )
            """), {"id": campaign_id})
        return campaign_id

async def list_broadcasts():
    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT b.id, b.body, b.audience, b.status, b.created_at, b.completed_at,
                   COUNT(r.telegram_id) AS total,
                   COUNT(*) FILTER (WHERE r.status='sent') AS sent,
                   COUNT(*) FILTER (WHERE r.status='failed') AS failed,
                   COUNT(*) FILTER (WHERE r.status='pending') AS pending
            FROM broadcasts b LEFT JOIN broadcast_recipients r ON r.broadcast_id=b.id
            GROUP BY b.id ORDER BY b.id DESC LIMIT 30
        """))).mappings().all()
        return [dict(r) for r in rows]

async def claim_recipient():
    """Atomic claim. Processing state survives until acknowledged."""
    async with SessionLocal() as session:
        async with session.begin():
            row = (await session.execute(text("""
                WITH picked AS (
                    SELECT r.broadcast_id, r.telegram_id
                    FROM broadcast_recipients r
                    JOIN broadcasts b ON b.id=r.broadcast_id
                    WHERE r.status='pending' AND b.status IN ('queued', 'sending')
                      AND (b.scheduled_at IS NULL OR b.scheduled_at <= NOW())
                    ORDER BY b.id, r.telegram_id
                    FOR UPDATE OF r SKIP LOCKED LIMIT 1
                )
                UPDATE broadcast_recipients r SET status='processing'
                FROM picked
                WHERE r.broadcast_id=picked.broadcast_id AND r.telegram_id=picked.telegram_id
                RETURNING r.broadcast_id, r.telegram_id
            """))).first()
            if not row:
                return None
            await session.execute(text("UPDATE broadcasts SET status='sending' WHERE id=:id"), {"id": row.broadcast_id})
            msg = (await session.execute(text("""
                SELECT b.body, b.campaign_kind, u.first_name
                FROM broadcasts b JOIN users u ON u.telegram_id=:uid
                WHERE b.id=:id
            """), {"id": row.broadcast_id, "uid": row.telegram_id})).one()
            body, kind, first_name = msg
            opted_out = (await session.execute(text(
                "SELECT EXISTS (SELECT 1 FROM broadcast_opt_out WHERE telegram_id=:id)"
            ), {"id": row.telegram_id})).scalar_one()
            if kind.startswith("reminder:"):
                trigger = kind.partition(":")[2]
                still_eligible = (await session.execute(text("""
                    SELECT CASE
                      WHEN :trigger='no_offer_24h' THEN NOT EXISTS (
                        SELECT 1 FROM offer_acceptances WHERE telegram_id=:uid
                      )
                      WHEN :trigger='no_click_24h' THEN NOT EXISTS (
                        SELECT 1 FROM subscription_clicks WHERE telegram_id=:uid
                      )
                      ELSE FALSE END
                """), {"trigger": trigger, "uid": row.telegram_id})).scalar_one()
                opted_out = opted_out or not still_eligible
            body = body.replace("{first_name}", first_name or "друг")
            return row.broadcast_id, row.telegram_id, body, opted_out

async def finish_recipient(campaign_id, user_id, status, error_code=None):
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE broadcast_recipients SET status=:status, error_code=:error,
                    sent_at=CASE WHEN :status='sent' THEN NOW() ELSE NULL END
                WHERE broadcast_id=:bid AND telegram_id=:uid
            """), {"status": status, "error": error_code, "bid": campaign_id, "uid": user_id})
            await session.execute(text("""
                UPDATE broadcasts SET status='completed', completed_at=NOW()
                WHERE id=:bid AND NOT EXISTS (
                    SELECT 1 FROM broadcast_recipients
                    WHERE broadcast_id=:bid AND status IN ('pending','processing')
                )
            """), {"bid": campaign_id})

async def mark_unfinished_as_uncertain():
    """A crash between sending and saving may leave unknown delivery; never auto-resend."""
    async with SessionLocal() as session:
        await session.execute(text("""
            UPDATE broadcast_recipients SET status='uncertain', error_code='interrupted'
            WHERE status='processing'
        """))
        await session.execute(text("""
            UPDATE broadcasts SET status='completed', completed_at=NOW()
            WHERE status IN ('queued','sending') AND NOT EXISTS (
                SELECT 1 FROM broadcast_recipients r
                WHERE r.broadcast_id=broadcasts.id AND r.status IN ('pending','processing')
            )
        """))
        await session.commit()
