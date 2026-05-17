import os
import asyncio
from admin.main import app
from bot.main import main as bot_main
import uvicorn


async def start_bot():
    await bot_main()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(start_bot())

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )