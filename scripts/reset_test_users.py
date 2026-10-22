"""Reset ONLY Telegram onboarding test data. Run manually after making a database backup.

Usage:
    /opt/dato-telegram/.venv/bin/python -m scripts.reset_test_users

This does not affect admin accounts, bot settings, or the website database.
"""
import asyncio
from sqlalchemy import text
from db.database import engine


async def main():
    tables = ("subscription_clicks", "offer_acceptances", "users")
    async with engine.begin() as conn:
        for table in tables:
            exists = await conn.scalar(
                text("SELECT to_regclass(:name) IS NOT NULL"),
                {"name": "public." + table},
            )
            if not exists:
                raise RuntimeError(f"Table missing: {table}. Aborting reset.")
        await conn.execute(text(
            "TRUNCATE TABLE subscription_clicks, offer_acceptances, users "
            "RESTART IDENTITY"
        ))
    print("Готово: регистрации, принятия оферты и клики сброшены. Настройки сохранены.")


if __name__ == "__main__":
    asyncio.run(main())
