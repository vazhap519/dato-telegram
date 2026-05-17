import asyncio
from sqlalchemy import text
from db.database import engine
from db.models import Base
from admin.auth import hash_password


async def init():
    async with engine.begin() as conn:
        # 🔧 create tables
        await conn.run_sync(Base.metadata.create_all)

        # 🔐 create admin თუ არ არსებობს
        result = await conn.execute(text("SELECT * FROM admins LIMIT 1"))
        admin = result.first()

        if not admin:
            await conn.execute(text("""
                INSERT INTO admins (username, password)
                VALUES (:u, :p)
            """), {
                "u": "admin",
                "p": hash_password("admin123")
            })

            print("✅ Admin created: admin / admin123")

        # ⚙️ create settings თუ არ არსებობს
        result = await conn.execute(text("SELECT * FROM settings LIMIT 1"))
        settings = result.first()

        if not settings:
            await conn.execute(text("""
                INSERT INTO settings (
                    id, video_url, text1, text2,
                    btn1_text, btn1_url,
                    btn2_text, btn2_url
                ) VALUES (
                    1,
                    'https://www.w3schools.com/html/mov_bbb.mp4',
                    'Hello 1',
                    'Hello 2',
                    'Button 1',
                    'https://example.com',
                    'Button 2',
                    'https://google.com'
                )
            """))

            print("✅ Default settings created")


if __name__ == "__main__":
    asyncio.run(init())