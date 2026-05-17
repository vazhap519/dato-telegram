from sqlalchemy import text
from .database import SessionLocal


async def add_user(user_id: int):
    async with SessionLocal() as session:
        await session.execute(
            text("""
            INSERT INTO users (telegram_id)
            VALUES (:user_id)
            ON CONFLICT DO NOTHING
            """),
            {"user_id": user_id}
        )
        await session.commit()


async def get_users_count():
    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM users")
        )
        return result.scalar()


# 🔥 ეს არის მთავარი FIX
async def get_settings():
    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM settings LIMIT 1")
        )
        row = result.fetchone()

        if row:
            return row._mapping  # ✅ dict-like object (ძალიან მნიშვნელოვანია)

        return None