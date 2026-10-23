"""User support tickets: persistent drafts, conversations and admin workflow."""
from sqlalchemy import text
from db.database import engine, SessionLocal

CATEGORIES = {
    "access": "Доступ и приглашения",
    "payment": "Оплата и подписка",
    "account": "Учётная запись",
    "other": "Другой вопрос",
}
STATUSES = {"open":"Открыто", "pending":"Ожидает ответа", "answered":"Ответ отправлен", "closed":"Закрыто"}

async def initialize_support():
    async with engine.begin() as c:
        await c.execute(text("""
        CREATE TABLE IF NOT EXISTS support_tickets (
          id BIGSERIAL PRIMARY KEY,
          telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
          category TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          closed_at TIMESTAMPTZ,
          last_actor TEXT NOT NULL DEFAULT 'user'
        )"""))
        await c.execute(text("""
        CREATE INDEX IF NOT EXISTS support_tickets_queue_idx
          ON support_tickets(status, updated_at DESC)
        """))
        await c.execute(text("""
        CREATE TABLE IF NOT EXISTS support_messages (
          id BIGSERIAL PRIMARY KEY,
          ticket_id BIGINT NOT NULL REFERENCES support_tickets(id),
          actor TEXT NOT NULL,
          body TEXT NOT NULL,
          delivery_status TEXT NOT NULL DEFAULT 'sent',
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
        await c.execute(text("""
        CREATE INDEX IF NOT EXISTS support_messages_ticket_idx
          ON support_messages(ticket_id,id)
        """))
        await c.execute(text("""
        CREATE TABLE IF NOT EXISTS support_chat_modes (
          telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
          active BOOLEAN NOT NULL DEFAULT FALSE,
          ticket_id BIGINT REFERENCES support_tickets(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
        await c.execute(text("""
        ALTER TABLE support_messages
        ADD COLUMN IF NOT EXISTS media_type TEXT NOT NULL DEFAULT 'text'
        """))
        await c.execute(text("""
        ALTER TABLE support_messages
        ADD COLUMN IF NOT EXISTS file_id TEXT
        """))
        await c.execute(text("""
        CREATE TABLE IF NOT EXISTS support_drafts (
          telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
          category TEXT,
          ticket_id BIGINT REFERENCES support_tickets(id),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))

async def start_draft(uid, category=None, ticket_id=None):
    if category not in CATEGORIES and ticket_id is None:
        raise ValueError("Неизвестная тема обращения")
    async with SessionLocal() as s:
        async with s.begin():
            if ticket_id is not None:
                row=(await s.execute(text("""
                    SELECT id FROM support_tickets
                    WHERE id=:ticket AND telegram_id=:uid AND status<>'closed'
                """),{"ticket":ticket_id,"uid":uid})).first()
                if not row: raise ValueError("Обращение закрыто или не найдено")
            await s.execute(text("""
                INSERT INTO support_drafts(telegram_id,category,ticket_id)
                VALUES (:uid,:category,:ticket)
                ON CONFLICT(telegram_id) DO UPDATE SET category=:category,
                    ticket_id=:ticket,updated_at=NOW()
            """),{"uid":uid,"category":category,"ticket":ticket_id})

async def cancel_draft(uid):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("DELETE FROM support_drafts WHERE telegram_id=:uid"),{"uid":uid})

async def save_user_message(uid, body):
    body=body.strip()
    if not body or len(body)>3000: raise ValueError("Сообщение должно содержать от 1 до 3000 символов")
    async with SessionLocal() as s:
        async with s.begin():
            draft=(await s.execute(text("""
                SELECT category,ticket_id FROM support_drafts WHERE telegram_id=:uid FOR UPDATE
            """),{"uid":uid})).mappings().first()
            if not draft: return None
            ticket=draft["ticket_id"]
            if ticket is None:
                ticket=(await s.execute(text("""
                    INSERT INTO support_tickets(telegram_id,category,status)
                    VALUES (:uid,:category,'open') RETURNING id
                """),{"uid":uid,"category":draft["category"]})).scalar_one()
            else:
                updated=await s.execute(text("""
                    UPDATE support_tickets SET updated_at=NOW(),status='open',
                        last_actor='user'
                    WHERE id=:ticket AND telegram_id=:uid AND status<>'closed'
                    RETURNING id
                """),{"ticket":ticket,"uid":uid})
                if not updated.first(): raise ValueError("Обращение уже закрыто")
            await s.execute(text("""
                INSERT INTO support_messages(ticket_id,actor,body)
                VALUES (:ticket,'user',:body)
            """),{"ticket":ticket,"body":body})
            await s.execute(text("""
                UPDATE support_tickets SET status='open',last_actor='user',updated_at=NOW()
                WHERE id=:ticket
            """),{"ticket":ticket})
            await s.execute(text("DELETE FROM support_drafts WHERE telegram_id=:uid"),{"uid":uid})
            return ticket

async def draft_exists(uid):
    async with SessionLocal() as s:
        return bool((await s.execute(text("""
            SELECT 1 FROM support_drafts WHERE telegram_id=:uid
        """),{"uid":uid})).first())

async def user_tickets(uid):
    async with SessionLocal() as s:
        return [dict(x) for x in (await s.execute(text("""
            SELECT id,category,status,updated_at FROM support_tickets
            WHERE telegram_id=:uid ORDER BY updated_at DESC LIMIT 15
        """),{"uid":uid})).mappings().all()]

async def ticket_detail(ticket_id, uid=None):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT t.*,u.username,u.first_name FROM support_tickets t
            LEFT JOIN users u ON u.telegram_id=t.telegram_id
            WHERE t.id=:ticket AND (CAST(:uid AS BIGINT) IS NULL OR t.telegram_id=:uid)
        """),{"ticket":ticket_id,"uid":uid})).mappings().first()
        if not row: return None,None
        messages=(await s.execute(text("""
            SELECT * FROM support_messages WHERE ticket_id=:ticket
            ORDER BY id DESC LIMIT 100
        """),{"ticket":ticket_id})).mappings().all()
        return dict(row),[dict(m) for m in reversed(messages)]

async def list_tickets(status="",search="",page=1):
    if status not in STATUSES: status=""
    page=max(1,page)
    term=(search or "").strip()[:100]
    async with SessionLocal() as s:
        criteria="""WHERE (:status='' OR t.status=:status)
          AND (:term='' OR CAST(t.id AS TEXT)=:term
            OR CAST(t.telegram_id AS TEXT)=:term
            OR u.username ILIKE :like OR u.first_name ILIKE :like)"""
        params={"status":status,"term":term,"like":"%"+term+"%","offset":(page-1)*30}
        count=(await s.execute(text("""
            SELECT COUNT(*) FROM support_tickets t
            LEFT JOIN users u ON u.telegram_id=t.telegram_id
        """+criteria),params)).scalar_one()
        rows=(await s.execute(text("""
            SELECT t.*,u.username,u.first_name,
            (SELECT COUNT(*) FROM support_messages m WHERE m.ticket_id=t.id) AS message_count
            FROM support_tickets t LEFT JOIN users u ON u.telegram_id=t.telegram_id
        """+criteria+""" ORDER BY CASE WHEN t.status='open' THEN 0
                   WHEN t.status='pending' THEN 1 WHEN t.status='answered' THEN 2 ELSE 3 END,
                   t.updated_at DESC LIMIT 30 OFFSET :offset"""),params)).mappings().all()
        return [dict(x) for x in rows],count

async def support_counts():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT status,COUNT(*) total FROM support_tickets GROUP BY status
        """))).all()
        return {k:v for k,v in rows}

async def admin_reply(ticket_id,actor,body):
    body=body.strip()
    if not body or len(body)>3000: raise ValueError("Ответ должен содержать от 1 до 3000 символов")
    async with SessionLocal() as s:
        async with s.begin():
            ticket=(await s.execute(text("""
                SELECT telegram_id FROM support_tickets
                WHERE id=:id AND status<>'closed' FOR UPDATE
            """),{"id":ticket_id})).mappings().first()
            if not ticket: raise ValueError("Обращение закрыто или не найдено")
            mid=(await s.execute(text("""
                INSERT INTO support_messages(ticket_id,actor,body,delivery_status)
                VALUES (:ticket,:actor,:body,'pending')
                RETURNING id
            """),{"ticket":ticket_id,"actor":"admin:"+actor,"body":body})).scalar_one()
            await s.execute(text("""
                UPDATE support_tickets SET status='answered',last_actor='admin',updated_at=NOW()
                WHERE id=:id
            """),{"id":ticket_id})
            return ticket["telegram_id"],mid

async def mark_delivery(message_id,success):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE support_messages SET delivery_status=:status WHERE id=:id
            """),{"id":message_id,"status":"sent" if success else "failed"})

async def set_ticket_status(ticket_id,status):
    if status not in ("open","pending","closed"): raise ValueError("Недопустимый статус")
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
                UPDATE support_tickets
                SET status=:status,closed_at=CASE WHEN :status='closed' THEN NOW() ELSE NULL END,
                  updated_at=NOW(),last_actor='admin'
                WHERE id=:ticket RETURNING telegram_id
            """),{"ticket":ticket_id,"status":status})).first()
            if not row: raise ValueError("Обращение не найдено")
            if status == "closed":
                await s.execute(text("""
                    UPDATE support_chat_modes SET active=FALSE,updated_at=NOW()
                    WHERE telegram_id=:uid AND ticket_id=:ticket
                """),{"uid":row[0],"ticket":ticket_id})
            return row[0]


async def failed_support_message(ticket_id,message_id):
    """Only retry a previously failed admin reply, never a delivered message."""
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT m.body,t.telegram_id
            FROM support_messages m JOIN support_tickets t ON t.id=m.ticket_id
            WHERE m.id=:message AND m.ticket_id=:ticket
              AND m.actor LIKE 'admin:%' AND m.delivery_status='failed'
        """),{"message":message_id,"ticket":ticket_id})).mappings().first()
        return dict(row) if row else None


async def start_support_chat(uid, ticket_id=None):
    """One active conversation at a time; old tickets remain browsable."""
    async with SessionLocal() as s:
        async with s.begin():
            if ticket_id is not None:
                row=(await s.execute(text("""
                    SELECT id FROM support_tickets
                    WHERE id=:ticket AND telegram_id=:uid AND status<>'closed'
                """),{"uid":uid,"ticket":ticket_id})).first()
                if not row: raise ValueError("Диалог закрыт")
            else:
                row=(await s.execute(text("""
                    SELECT id FROM support_tickets
                    WHERE telegram_id=:uid AND status<>'closed'
                    ORDER BY updated_at DESC LIMIT 1
                """),{"uid":uid})).first()
                ticket_id=row[0] if row else None
            await s.execute(text("""
                INSERT INTO support_chat_modes(telegram_id,active,ticket_id)
                VALUES (:uid,TRUE,:ticket)
                ON CONFLICT(telegram_id) DO UPDATE
                  SET active=TRUE,ticket_id=EXCLUDED.ticket_id,updated_at=NOW()
            """),{"uid":uid,"ticket":ticket_id})
            await s.execute(text("DELETE FROM support_drafts WHERE telegram_id=:uid"),{"uid":uid})
            return ticket_id


async def stop_support_chat(uid):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE support_chat_modes SET active=FALSE,updated_at=NOW()
                WHERE telegram_id=:uid
            """),{"uid":uid})
            await s.execute(text("DELETE FROM support_drafts WHERE telegram_id=:uid"),{"uid":uid})


async def chat_is_active(uid):
    async with SessionLocal() as s:
        return bool((await s.execute(text("""
            SELECT 1 FROM support_chat_modes
            WHERE telegram_id=:uid AND active=TRUE
        """),{"uid":uid})).first())


async def add_support_chat_message(uid,body,media_type="text",file_id=None):
    body=(body or "").strip()
    if media_type not in ("text","photo","voice","document"):
        raise ValueError("Неподдерживаемый формат")
    if len(body)>3000:
        raise ValueError("Сообщение слишком длинное — максимум 3000 символов")
    if media_type=="text" and not body:
        raise ValueError("Напишите сообщение")
    if media_type!="text" and not file_id:
        raise ValueError("Не найден вложенный файл")
    async with SessionLocal() as s:
        async with s.begin():
            mode=(await s.execute(text("""
                SELECT ticket_id FROM support_chat_modes
                WHERE telegram_id=:uid AND active=TRUE FOR UPDATE
            """),{"uid":uid})).mappings().first()
            if not mode: return None
            ticket_id=mode["ticket_id"]
            if ticket_id is not None:
                ticket=(await s.execute(text("""
                    SELECT id FROM support_tickets WHERE id=:ticket AND telegram_id=:uid
                    AND status<>'closed' FOR UPDATE
                """),{"uid":uid,"ticket":ticket_id})).first()
                if not ticket: ticket_id=None
            if ticket_id is None:
                ticket_id=(await s.execute(text("""
                    INSERT INTO support_tickets(telegram_id,category,status)
                    VALUES (:uid,'other','open') RETURNING id
                """),{"uid":uid})).scalar_one()
                await s.execute(text("""
                    UPDATE support_chat_modes SET ticket_id=:ticket WHERE telegram_id=:uid
                """),{"uid":uid,"ticket":ticket_id})
            await s.execute(text("""
                INSERT INTO support_messages(ticket_id,actor,body,media_type,file_id)
                VALUES (:ticket,'user',:body,:kind,:file)
            """),{"ticket":ticket_id,"body":body,"kind":media_type,"file":file_id})
            await s.execute(text("""
                UPDATE support_tickets SET status='open',last_actor='user',
                    updated_at=NOW() WHERE id=:ticket
            """),{"ticket":ticket_id})
            return ticket_id


async def support_media_record(message_id):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
            SELECT file_id,media_type FROM support_messages
            WHERE id=:id AND file_id IS NOT NULL AND actor='user'
        """),{"id":message_id})).mappings().first()
        return dict(row) if row else None
