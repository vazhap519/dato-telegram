"""Private, one-use entry links for checking the real Tribute checkout via our bot.

This is NOT an entitlement/payment processor. No grants or subscription updates.
"""
import hashlib
import re
import secrets

TEST_SUBSCRIPTION_ID = 279302
TEST_PERIOD_ID = 528940
TEST_PRICE_LABEL = "1 USD / месяц"
TEST_PAYMENT_URL = "https://t.me/tribute/app?startapp=s1aES"
TEST_BROWSER_URL = "https://web.tribute.tg/s/1aES"
_TOKEN_RE = re.compile(r"^tt_[A-Za-z0-9_-]{32}$")


def valid_test_token(token: str) -> bool:
    return isinstance(token, str) and _TOKEN_RE.fullmatch(token) is not None


def hash_test_token(token: str) -> str:
    if not valid_test_token(token):
        raise ValueError("Invalid test token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


async def initialize_tribute_payment_tests():
    from sqlalchemy import text
    from db.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_payment_test_links (
                id BIGSERIAL PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                created_by TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                expires_at TIMESTAMPTZ NOT NULL,
                claimed_at TIMESTAMPTZ,
                claimed_by BIGINT,
                CHECK ((claimed_at IS NULL) = (claimed_by IS NULL))
            )
        """))
        await conn.execute(text("""
            CREATE INDEX IF NOT EXISTS tribute_test_links_created_idx
            ON tribute_payment_test_links (created_at DESC)
        """))


async def create_tribute_test_link(admin_name: str) -> str:
    """Return plaintext deep-link payload once; store only its SHA-256 digest."""
    from sqlalchemy import text
    from db.database import SessionLocal
    if not isinstance(admin_name, str) or not 1 <= len(admin_name) <= 100:
        raise ValueError("Invalid admin")
    token = "tt_" + secrets.token_urlsafe(24)
    assert valid_test_token(token)
    async with SessionLocal() as session:
        async with session.begin():
            await session.execute(text("""
                INSERT INTO tribute_payment_test_links(token_hash, created_by, expires_at)
                VALUES (:digest, :actor, NOW() + INTERVAL '20 minutes')
            """), {"digest": hash_test_token(token), "actor": admin_name})
    return token


async def claim_tribute_test_link(token: str, telegram_id: int) -> bool:
    """Atomic single-use claim; never touches payment or group-access tables."""
    from sqlalchemy import text
    from db.database import SessionLocal
    if not valid_test_token(token) or type(telegram_id) is not int or telegram_id <= 0:
        return False
    async with SessionLocal() as session:
        async with session.begin():
            row = (await session.execute(text("""
                UPDATE tribute_payment_test_links
                SET claimed_by=:uid, claimed_at=NOW()
                WHERE token_hash=:digest
                  AND claimed_at IS NULL
                  AND expires_at>NOW()
                RETURNING id
            """), {"uid":telegram_id, "digest":hash_test_token(token)})).first()
    return row is not None


async def recent_tribute_test_links(limit: int = 20) -> list[dict]:
    from sqlalchemy import text
    from db.database import SessionLocal
    async with SessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT id, created_by, created_at, expires_at, claimed_at, claimed_by
            FROM tribute_payment_test_links
            ORDER BY id DESC
            LIMIT :limit
        """), {"limit":min(max(limit, 1), 40)})).mappings().all()
    return [dict(row) for row in rows]
