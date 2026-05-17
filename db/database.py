# import os
# from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
# from sqlalchemy.orm import sessionmaker
# from dotenv import load_dotenv

# load_dotenv()

# engine = create_async_engine(os.getenv("DATABASE_URL"))
# SessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

DATABASE_URL = "sqlite+aiosqlite:///database.db"

engine = create_async_engine(DATABASE_URL)

SessionLocal = sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)