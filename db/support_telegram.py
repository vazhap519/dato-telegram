"""Private Telegram interface for existing support staff.

Binding requires a short-lived code generated inside Telegram and confirmed
from an already-authenticated staff web session. No public /admin password.
"""
import hashlib
import secrets

from sqlalchemy import text
from db.database import SessionLocal, engine

SUPPORT_ROLES = ("owner", "admin", "support")


async def initialize_support_telegram():
    async with engine.begin() as c:
        await c.execute(text("""
          CREATE TABLE IF NOT EXISTS support_telegram_links (
            admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE,
            telegram_id BIGINT NOT NULL UNIQUE,
            notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE,
            notifications_since TIMESTAMPTZ,
            reply_ticket_id BIGINT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
          )
        """))
        await c.execute(text("""
          CREATE TABLE IF NOT EXISTS support_telegram_pairing (
            telegram_id BIGINT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMPTZ NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
          )
        """))
        await c.execute(text("""
          CREATE TABLE IF NOT EXISTS support_telegram_notifications (
            admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
            message_id BIGINT NOT NULL REFERENCES support_messages(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(admin_id, message_id)
          )
        """))


async def generate_pair_code(telegram_id):
    code = str(secrets.randbelow(90000000) + 10000000)
    hashed = hashlib.sha256(code.encode()).hexdigest()
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
              INSERT INTO support_telegram_pairing(telegram_id,code_hash,expires_at,attempts)
              VALUES (:uid,:hash,NOW() + INTERVAL '10 minutes',0)
              ON CONFLICT(telegram_id) DO UPDATE
                SET code_hash=EXCLUDED.code_hash,expires_at=EXCLUDED.expires_at,attempts=0
            """), {"uid":telegram_id,"hash":hashed})
    return code


async def link_staff(username, code):
    if len(code)!=8 or not code.isdecimal():
        raise ValueError("Код должен состоять из восьми цифр")
    hashed = hashlib.sha256(code.encode()).hexdigest()
    async with SessionLocal() as s:
        async with s.begin():
            account=(await s.execute(text("""
              SELECT id,role,enabled FROM admins WHERE username=:name FOR UPDATE
            """),{"name":username})).mappings().first()
            if not account or not account["enabled"] or account["role"] not in SUPPORT_ROLES:
                raise ValueError("Нет доступа к техподдержке")
            pairing=(await s.execute(text("""
              SELECT telegram_id FROM support_telegram_pairing
              WHERE code_hash=:hash AND expires_at>NOW() AND attempts<5
              FOR UPDATE
            """),{"hash":hashed})).mappings().first()
            if not pairing:
                raise ValueError("Код не найден или срок действия истёк")
            taken=(await s.execute(text("""
              SELECT admin_id FROM support_telegram_links
              WHERE telegram_id=:uid AND admin_id<>:admin
            """),{"uid":pairing["telegram_id"],"admin":account["id"]})).first()
            if taken:
                raise ValueError("Этот Telegram уже привязан к другой учётной записи")
            await s.execute(text("""
              INSERT INTO support_telegram_links(admin_id,telegram_id,notifications_enabled,reply_ticket_id)
              VALUES (:admin,:uid,FALSE,NULL)
              ON CONFLICT(admin_id) DO UPDATE SET telegram_id=EXCLUDED.telegram_id,
                notifications_enabled=FALSE,notifications_since=NULL,
                reply_ticket_id=NULL,updated_at=NOW()
            """),{"admin":account["id"],"uid":pairing["telegram_id"]})
            await s.execute(text("DELETE FROM support_telegram_pairing WHERE telegram_id=:uid"),
                            {"uid":pairing["telegram_id"]})
            return pairing["telegram_id"]


async def staff_link(username):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
          SELECT l.telegram_id,l.notifications_enabled,a.role
          FROM admins a LEFT JOIN support_telegram_links l ON l.admin_id=a.id
          WHERE a.username=:name
        """),{"name":username})).mappings().first()
        return dict(row) if row else None


async def configure_staff(username, notifications_enabled=False, unlink=False):
    async with SessionLocal() as s:
        async with s.begin():
            account=(await s.execute(text("""
              SELECT a.id,a.role,a.enabled,l.telegram_id
              FROM admins a JOIN support_telegram_links l ON l.admin_id=a.id
              WHERE a.username=:name FOR UPDATE OF l
            """),{"name":username})).mappings().first()
            if not account or not account["enabled"] or account["role"] not in SUPPORT_ROLES:
                raise ValueError("Telegram не привязан или учётная запись отключена")
            if unlink:
                await s.execute(text("DELETE FROM support_telegram_links WHERE admin_id=:id"),
                                {"id":account["id"]})
            else:
                await s.execute(text("""
                  UPDATE support_telegram_links
                  SET notifications_enabled=:value,
                    notifications_since=CASE WHEN :value
                      THEN CASE WHEN notifications_enabled THEN notifications_since ELSE NOW() END
                      ELSE NULL END,
                    updated_at=NOW()
                  WHERE admin_id=:id
                """),{"id":account["id"],"value":bool(notifications_enabled)})


async def telegram_staff(telegram_id):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
          SELECT a.id,a.username,a.role,l.reply_ticket_id,l.notifications_enabled
          FROM support_telegram_links l JOIN admins a ON a.id=l.admin_id
          WHERE l.telegram_id=:uid AND a.enabled=TRUE
            AND a.role IN ('owner','admin','support')
        """),{"uid":telegram_id})).mappings().first()
        return dict(row) if row else None


async def set_reply_target(telegram_id,ticket_id):
    async with SessionLocal() as s:
        async with s.begin():
            if ticket_id is not None:
                present=(await s.execute(text("""
                  SELECT 1 FROM support_tickets WHERE id=:ticket AND status<>'closed'
                """),{"ticket":ticket_id})).first()
                if not present: raise ValueError("Диалог закрыт или не найден")
            await s.execute(text("""
              UPDATE support_telegram_links SET reply_ticket_id=:ticket,updated_at=NOW()
              WHERE telegram_id=:uid
            """),{"ticket":ticket_id,"uid":telegram_id})


async def pending_notifications(limit=30):
    """Claim after opt-in, never replay historical tickets on activation."""
    async with SessionLocal() as s:
        async with s.begin():
            rows=(await s.execute(text("""
              WITH picked AS (
                SELECT a.id AS admin_id,l.telegram_id,m.id AS message_id,
                       t.id AS ticket_id,t.telegram_id AS user_id,m.body,m.media_type,
                       m.telegram_message_id,u.first_name,u.username
                FROM support_telegram_links l
                JOIN admins a ON a.id=l.admin_id
                JOIN support_messages m ON m.actor='user'
                  AND m.created_at >= l.notifications_since
                JOIN support_tickets t ON t.id=m.ticket_id
                LEFT JOIN users u ON u.telegram_id=t.telegram_id
                WHERE a.enabled=TRUE AND a.role IN ('owner','admin','support')
                  AND l.notifications_enabled=TRUE
                  AND NOT EXISTS (
                    SELECT 1 FROM support_telegram_notifications n
                    WHERE n.admin_id=a.id AND n.message_id=m.id
                  )
                ORDER BY m.id LIMIT :limit
              ), reserved AS (
                INSERT INTO support_telegram_notifications(admin_id,message_id,status)
                SELECT admin_id,message_id,'pending' FROM picked
                ON CONFLICT DO NOTHING RETURNING admin_id,message_id
              )
              SELECT picked.* FROM picked JOIN reserved USING(admin_id,message_id)
            """),{"limit":limit})).mappings().all()
            return [dict(row) for row in rows]


async def mark_notification(admin_id,message_id,sent):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
              UPDATE support_telegram_notifications SET status=:status
              WHERE admin_id=:aid AND message_id=:mid
            """),{"aid":admin_id,"mid":message_id,"status":"sent" if sent else "failed"})
