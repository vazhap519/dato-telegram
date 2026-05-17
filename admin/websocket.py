connections = []

async def notify_all(message: str):
    for conn in connections:
        await conn.send_text(message)