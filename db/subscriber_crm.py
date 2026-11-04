"""Subscriber CRM: private admin-to-user messages and unified audit timeline.

Only verified payment receipts count as purchases. Personal messages are not
support-ticket replies, and failed Telegram deliveries remain visible.
"""
import re
from sqlalchemy import text
from db.database import engine,SessionLocal

async def initialize_crm():
    async with engine.begin() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS subscriber_direct_messages (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                actor TEXT NOT NULL,
                message TEXT NOT NULL,
                request_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','sent','failed')),
                error_reason TEXT,
                telegram_message_id BIGINT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
        """))
        await db.execute(text("""
            ALTER TABLE subscriber_direct_messages
            ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ
        """))
        await db.execute(text("""
            ALTER TABLE subscriber_direct_messages
            ADD COLUMN IF NOT EXISTS deleted_by TEXT
        """))
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS subscriber_crm_history_visibility (
                telegram_id BIGINT PRIMARY KEY REFERENCES users(telegram_id),
                cleared_before TIMESTAMPTZ NOT NULL,
                cleared_by TEXT NOT NULL
            )
        """))
        await db.execute(text("""
            CREATE INDEX IF NOT EXISTS subscriber_direct_messages_user_idx
            ON subscriber_direct_messages(telegram_id,created_at DESC)
        """))

async def begin_direct_message(uid,actor,body,request_key):
    body=(body or "").strip()
    if not 1<=len(body)<=2000:
        raise ValueError("Сообщение должно содержать от 1 до 2000 символов")
    if not re.fullmatch(r"[a-f0-9]{32}",request_key or ""):
        raise ValueError("Недопустимый идентификатор отправки")
    async with SessionLocal() as db:
        async with db.begin():
            exists=(await db.execute(text("""
                SELECT 1 FROM users WHERE telegram_id=:uid
            """),{"uid":uid})).first()
            if not exists: raise ValueError("Участник не найден")
            record=(await db.execute(text("""
                INSERT INTO subscriber_direct_messages(telegram_id,actor,message,request_key)
                VALUES(:uid,:actor,:message,:key)
                ON CONFLICT(request_key) DO NOTHING RETURNING id
            """),{"uid":uid,"actor":actor,"message":body,"key":request_key})).scalar_one_or_none()
            return record

async def finish_direct_message(mid,success,telegram_message_id=None,error=None):
    async with SessionLocal() as db:
        async with db.begin():
            await db.execute(text("""
                UPDATE subscriber_direct_messages SET
                  status=:status,telegram_message_id=:message_id,
                  error_reason=:error,finished_at=NOW()
                WHERE id=:id AND status='pending'
            """),{"status":"sent" if success else "failed","message_id":telegram_message_id,
                  "error":(error or "")[:240] if error else None,"id":mid})

async def crm_timeline(uid,limit=60):
    """Events are read-only; neither pending orders nor webhook receipts claim payment."""
    async with SessionLocal() as db:
        rows=(await db.execute(text("""
            SELECT happened_at,kind,description,actor,status,detail FROM (
                SELECT a.created_at happened_at,'access'::text kind,
                       a.action description,a.actor, ''::text status,
                       ''::text detail
                FROM subscription_audit_log a WHERE a.telegram_id=:uid
                  AND a.action<>'crm_history_cleared'
                UNION ALL
                SELECT v.verified_at,'payment',
                       'Платёж · ' || v.provider, v.provider,
                       v.payment_status,COALESCE(v.amount::text || ' ' || v.currency,'')
                FROM verified_payment_receipts v WHERE v.telegram_id=:uid
                UNION ALL
                SELECT t.created_at,'support','Создано обращение',
                       'Участник',t.status,''
                FROM support_tickets t WHERE t.telegram_id=:uid
                UNION ALL
                SELECT m.created_at,'support_message','Сообщение поддержки',
                       m.actor,m.delivery_status,
                       CASE WHEN m.media_type='text' THEN LEFT(m.body,180)
                            ELSE 'Вложение: ' || m.media_type END
                FROM support_messages m
                JOIN support_tickets t ON t.id=m.ticket_id
                WHERE t.telegram_id=:uid
                UNION ALL
                SELECT d.created_at,'direct','Личное сообщение администратора',
                       d.actor,d.status,LEFT(d.message,180)
                FROM subscriber_direct_messages d WHERE d.telegram_id=:uid
                UNION ALL
                SELECT br.sent_at,'broadcast','Получена рассылка',
                       'Система',br.status,''
                FROM broadcast_recipients br WHERE br.telegram_id=:uid
                  AND br.sent_at IS NOT NULL
            ) timeline
            WHERE happened_at > COALESCE(
                (SELECT cleared_before FROM subscriber_crm_history_visibility
                 WHERE telegram_id=:uid),'-infinity'::timestamptz)
            ORDER BY happened_at DESC
            LIMIT :limit
        """),{"uid":uid,"limit":max(1,min(100,int(limit)))})).mappings().all()
        return [dict(x) for x in rows]

async def direct_message_history(uid,limit=25):
    async with SessionLocal() as db:
        rows=(await db.execute(text("""
            SELECT id,actor,message,status,error_reason,created_at,finished_at,
                   telegram_message_id,deleted_at,deleted_by
            FROM subscriber_direct_messages
            WHERE telegram_id=:uid AND created_at > COALESCE(
                (SELECT cleared_before FROM subscriber_crm_history_visibility
                 WHERE telegram_id=:uid),'-infinity'::timestamptz)
            ORDER BY id DESC LIMIT :limit
        """),{"uid":uid,"limit":limit})).mappings().all()
        return [dict(row) for row in rows]

async def crm_tags():
    async with SessionLocal() as db:
        rows=(await db.execute(text("""
            SELECT DISTINCT BTRIM(token) tag
            FROM subscriber_admin_profiles p,
                 LATERAL regexp_split_to_table(p.tags, ',') token
            WHERE BTRIM(token) <> ''
            ORDER BY tag LIMIT 150
        """))).all()
        return [x[0] for x in rows]


async def direct_message_for_delete(uid,mid):
    async with SessionLocal() as db:
        row=(await db.execute(text("""
            SELECT id,telegram_id,telegram_message_id,finished_at,status,deleted_at
            FROM subscriber_direct_messages
            WHERE id=:id AND telegram_id=:uid
        """),{"uid":uid,"id":mid})).mappings().first()
        return dict(row) if row else None

async def mark_direct_message_deleted(uid,mid,actor):
    async with SessionLocal() as db:
        async with db.begin():
            row=await db.execute(text("""
                UPDATE subscriber_direct_messages
                SET deleted_at=NOW(),deleted_by=:actor,
                    message='[Сообщение удалено из Telegram]',error_reason=NULL
                WHERE id=:id AND telegram_id=:uid AND deleted_at IS NULL
                RETURNING id
            """),{"id":mid,"uid":uid,"actor":actor})
            if row.first():
                await db.execute(text("""
                    INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                    VALUES (:uid,'crm_message_deleted',:actor,
                      jsonb_build_object('message_id',CAST(:mid AS BIGINT)))
                """),{"uid":uid,"actor":actor,"mid":mid})
                return True
            return False


async def clear_visible_crm_history(uid,actor):
    """Hide prior CRM history, retain original evidence and Telegram messages.

    This action intentionally does not modify support tickets, verified
    payments, subscription permissions, message delivery, or audit records.
    """
    async with SessionLocal() as db:
        async with db.begin():
            user=(await db.execute(text("""
                SELECT 1 FROM users WHERE telegram_id=:uid
            """),{"uid":uid})).first()
            if not user:
                raise ValueError("Пользователь не найден")
            await db.execute(text("""
                INSERT INTO subscriber_crm_history_visibility
                    (telegram_id,cleared_before,cleared_by)
                VALUES (:uid,NOW(),:actor)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    cleared_before=EXCLUDED.cleared_before,
                    cleared_by=EXCLUDED.cleared_by
            """),{"uid":uid,"actor":actor})
            await db.execute(text("""
                INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
                VALUES (:uid,'crm_history_cleared',:actor,
                        jsonb_build_object('scope','visible_crm_history'))
            """),{"uid":uid,"actor":actor})
