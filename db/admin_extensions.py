"""Operational CRM tasks, audit, search and error queue. No payment assumptions."""
from sqlalchemy import text
from db.database import engine,SessionLocal

async def initialize_extensions():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS crm_tasks(
              id BIGSERIAL PRIMARY KEY,telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
              title TEXT NOT NULL,details TEXT NOT NULL DEFAULT '',
              due_at TIMESTAMPTZ NOT NULL,assignee TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','done','cancelled')),
              remind BOOLEAN NOT NULL DEFAULT FALSE,
              notified_at TIMESTAMPTZ,claimed_at TIMESTAMPTZ,
              created_by TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              completed_at TIMESTAMPTZ,updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            CREATE INDEX IF NOT EXISTS crm_tasks_due_idx
            ON crm_tasks(status,due_at) 
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_operation_audit(
              id BIGSERIAL PRIMARY KEY,actor TEXT NOT NULL,action TEXT NOT NULL,
              target TEXT NOT NULL DEFAULT '',details TEXT NOT NULL DEFAULT '',
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS admin_issue_reviews(
              kind TEXT NOT NULL,item_id TEXT NOT NULL,
              telegram_id BIGINT NOT NULL,
              reviewed_by TEXT NOT NULL,reviewed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              note TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(kind,item_id,telegram_id)
            )
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS crm_task_reminder_attempts(
              task_id BIGINT PRIMARY KEY REFERENCES crm_tasks(id),
              state TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              error_code TEXT
            )
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS crm_task_notification_settings(
              id INTEGER PRIMARY KEY CHECK(id=1),
              enabled BOOLEAN NOT NULL DEFAULT FALSE,
              telegram_id BIGINT,
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))
        await c.execute(text("""
            INSERT INTO crm_task_notification_settings(id) VALUES (1)
            ON CONFLICT(id) DO NOTHING
        """))
async def audit(actor,action,target='',details=''):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
               INSERT INTO admin_operation_audit(actor,action,target,details)
               VALUES (:actor,:action,:target,:details)
            """),{"actor":actor,"action":action,"target":str(target)[:150],"details":str(details)[:500]})

async def create_task(uid,title,details,due_at,assignee,remind,actor):
    title=(title or '').strip();details=(details or '').strip();assignee=(assignee or '').strip()
    if not 1<=len(title)<=160 or len(details)>2000 or not 1<=len(assignee)<=100:
        raise ValueError("Проверьте название, описание и ответственного")
    async with SessionLocal() as s:
        async with s.begin():
            result=await s.execute(text("""
               INSERT INTO crm_tasks(telegram_id,title,details,due_at,assignee,remind,created_by)
               VALUES (:uid,:title,:details,:due,:assignee,:remind,:actor)
               RETURNING id
            """),{"uid":uid,"title":title,"details":details,"due":due_at,"assignee":assignee,
                  "remind":remind,"actor":actor})
            task_id=result.scalar_one()
            await s.execute(text("""
               INSERT INTO admin_operation_audit(actor,action,target)
               VALUES (:actor,'task_created',:target)
            """),{"actor":actor,"target":str(task_id)})
            return task_id

async def tasks(uid=None,status="open",limit=100):
    if status not in ('open','done','cancelled','all'): status='open'
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT t.*,u.first_name,u.username FROM crm_tasks t
          JOIN users u ON u.telegram_id=t.telegram_id
          WHERE (:uid IS NULL OR t.telegram_id=:uid)
            AND (:status='all' OR t.status=:status)
          ORDER BY CASE WHEN t.status='open' THEN 0 ELSE 1 END,
                   t.due_at ASC,t.id DESC LIMIT :limit
        """),{"uid":uid,"status":status,"limit":min(250,max(1,int(limit)))})).mappings().all()
        return [dict(x) for x in rows]

async def update_task(task_id,uid,status,actor):
    if status not in ('done','cancelled','open'): raise ValueError("Недопустимый статус")
    async with SessionLocal() as s:
        async with s.begin():
            record=(await s.execute(text("""
              UPDATE crm_tasks SET status=:status,updated_at=NOW(),
                completed_at=CASE WHEN :status='done' THEN NOW() ELSE NULL END,
                notified_at=CASE WHEN :status='open' THEN NULL ELSE notified_at END
              WHERE id=:id AND (:uid IS NULL OR telegram_id=:uid)
              RETURNING id,telegram_id
            """),{"id":task_id,"uid":uid,"status":status})).first()
            if not record:raise ValueError("Задача не найдена")
            await s.execute(text("""
              INSERT INTO admin_operation_audit(actor,action,target,details)
              VALUES (:actor,'task_status_changed',:target,:details)
            """),{"actor":actor,"target":str(task_id),"details":status})

async def task_notification_settings():
    async with SessionLocal() as s:
        row=(await s.execute(text("SELECT enabled,telegram_id FROM crm_task_notification_settings WHERE id=1"))).mappings().one()
        return dict(row)

async def update_task_notification_settings(enabled,telegram_id,actor):
    if enabled and (not telegram_id or telegram_id<=0):
        raise ValueError("Укажите Telegram ID для уведомлений")
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE crm_task_notification_settings
                SET enabled=:enabled,telegram_id=:uid,updated_at=NOW() WHERE id=1
            """),{"enabled":enabled,"uid":telegram_id})
    await audit(actor,"task_alert_settings")

async def claim_due_tasks():
    """At-most-once dispatch after durable claim; never duplicate on worker restart."""
    async with SessionLocal() as s:
        async with s.begin():
            rows=(await s.execute(text("""
                WITH picked AS (
                  SELECT id FROM crm_tasks WHERE status='open' AND remind=TRUE
                    AND due_at<=NOW() AND notified_at IS NULL
                  ORDER BY due_at LIMIT 10 FOR UPDATE SKIP LOCKED
                )
                UPDATE crm_tasks t SET notified_at=NOW(),claimed_at=NOW()
                FROM picked WHERE t.id=picked.id
                RETURNING t.id,t.telegram_id,t.title,t.assignee
            """))).mappings().all()
            return [dict(x) for x in rows]

async def log_task_notification(task_id,success,error=None):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                INSERT INTO crm_task_reminder_attempts(task_id,state,error_code)
                VALUES (:id,:state,:error) ON CONFLICT(task_id)
                DO UPDATE SET state=EXCLUDED.state,error_code=EXCLUDED.error_code
            """),{"id":task_id,"state":"sent" if success else "failed","error":str(error)[:100] if error else None})

async def operations(limit=80):
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT actor,action,target,details,created_at
          FROM admin_operation_audit ORDER BY id DESC LIMIT :limit
        """),{"limit":max(1,min(limit,200))})).mappings().all()
        return [dict(x) for x in rows]

async def global_search(query):
    q=(query or '').strip().lstrip('@')[:80]
    if len(q)<2:return {"people":[],"support":[],"payments":[]}
    params={"q":q}
    async with SessionLocal() as s:
        people=(await s.execute(text("""
          SELECT telegram_id,first_name,username FROM users
          WHERE telegram_id::text ILIKE '%'||:q||'%' OR
            COALESCE(first_name,'') ILIKE '%'||:q||'%' OR
            COALESCE(username,'') ILIKE '%'||:q||'%'
          ORDER BY telegram_id DESC LIMIT 20
        """),params)).mappings().all()
        support=(await s.execute(text("""
          SELECT t.id,t.telegram_id,t.status FROM support_tickets t
          JOIN users u ON u.telegram_id=t.telegram_id
          WHERE t.id::text ILIKE '%'||:q||'%' OR
            t.telegram_id::text ILIKE '%'||:q||'%' OR
            COALESCE(u.username,'') ILIKE '%'||:q||'%'
          ORDER BY t.id DESC LIMIT 20
        """),params)).mappings().all()
        payments=(await s.execute(text("""
          SELECT id,telegram_id,provider,provider_payment_id,payment_status
          FROM verified_payment_receipts
          WHERE telegram_id::text ILIKE '%'||:q||'%' OR
          provider_payment_id ILIKE '%'||:q||'%' OR provider ILIKE '%'||:q||'%'
          ORDER BY verified_at DESC LIMIT 20
        """),params)).mappings().all()
        return {"people":[dict(x) for x in people],"support":[dict(x) for x in support],
                "payments":[dict(x) for x in payments]}

async def failure_queue():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT 'message' kind,d.id::text item_id,d.telegram_id,
            d.error_reason detail,d.created_at occurred_at
          FROM subscriber_direct_messages d WHERE d.status='failed'
          UNION ALL
          SELECT 'broadcast',r.broadcast_id::text,r.telegram_id,
            r.error_code,r.sent_at
          FROM broadcast_recipients r WHERE r.status IN ('failed','uncertain')
          ORDER BY occurred_at DESC NULLS LAST LIMIT 100
        """))).mappings().all()
        reviewed=(await s.execute(text("""
          SELECT kind,item_id,telegram_id,reviewed_by,reviewed_at,note
          FROM admin_issue_reviews
        """))).mappings().all()
        mapping={(x["kind"],x["item_id"],x["telegram_id"]):dict(x) for x in reviewed}
        return [{**dict(x),"review":mapping.get((x["kind"],x["item_id"],x["telegram_id"]))}
                for x in rows]


async def bulk_update_tag(user_ids,tag,operation,actor):
    """Apply one tag to at most 50 explicitly selected subscribers."""
    if operation not in ("add","remove"):raise ValueError("Недопустимое действие")
    tag=(tag or "").strip()
    if not 1<=len(tag)<=40 or "," in tag or "\n" in tag:
        raise ValueError("Метка должна содержать 1–40 символов без запятых")
    ids=sorted(set(int(x) for x in user_ids))
    if not ids or len(ids)>50 or any(uid<=0 for uid in ids):
        raise ValueError("Выберите от 1 до 50 участников")
    async with SessionLocal() as s:
        async with s.begin():
            users=(await s.execute(text("""
              SELECT telegram_id FROM users WHERE telegram_id = ANY(:ids)
            """),{"ids":ids})).scalars().all()
            if len(users)!=len(ids):raise ValueError("Один из участников не найден")
            for uid in ids:
                await s.execute(text("""
                  INSERT INTO subscriber_admin_profiles(telegram_id,tags,updated_by)
                  VALUES (:uid,'',:actor)
                  ON CONFLICT(telegram_id) DO NOTHING
                """),{"uid":uid,"actor":actor})
            records=(await s.execute(text("""
              SELECT telegram_id,tags FROM subscriber_admin_profiles
              WHERE telegram_id = ANY(:ids) FOR UPDATE
            """),{"ids":ids})).mappings().all()
            for record in records:
                tags=[x.strip() for x in (record["tags"] or "").split(",") if x.strip()]
                if operation=="add" and tag.casefold() not in {x.casefold() for x in tags}:
                    tags.append(tag)
                if operation=="remove":
                    tags=[x for x in tags if x.casefold()!=tag.casefold()]
                if len(tags)>15:
                    raise ValueError("У одного из участников уже 15 меток")
                await s.execute(text("""
                    UPDATE subscriber_admin_profiles
                    SET tags=:tags,updated_by=:actor,updated_at=NOW()
                    WHERE telegram_id=:uid
                """),{"uid":record["telegram_id"],"tags":", ".join(tags),"actor":actor})
            await s.execute(text("""
              INSERT INTO admin_operation_audit(actor,action,target,details)
              VALUES (:actor,:action,:target,:detail)
            """),{"actor":actor,"action":"bulk_tag_"+operation,
                  "target":str(len(ids))+" subscribers","detail":tag})
            return len(ids)


async def mark_issue_review(kind,item_id,telegram_id,reviewed,note,actor):
    if kind not in ("message","broadcast") or not item_id.isdecimal() or telegram_id<=0:
        raise ValueError("Недопустимая операция")
    note=(note or "").strip()
    if len(note)>500:raise ValueError("Примечание слишком длинное")
    async with SessionLocal() as s:
        async with s.begin():
            if reviewed:
                await s.execute(text("""
                  INSERT INTO admin_issue_reviews(kind,item_id,telegram_id,reviewed_by,note)
                  VALUES(:kind,:id,:uid,:actor,:note)
                  ON CONFLICT(kind,item_id,telegram_id)
                  DO UPDATE SET reviewed_by=EXCLUDED.reviewed_by,
                    reviewed_at=NOW(),note=EXCLUDED.note
                """),{"kind":kind,"id":item_id,"uid":telegram_id,"actor":actor,"note":note})
            else:
                await s.execute(text("""
                  DELETE FROM admin_issue_reviews
                  WHERE kind=:kind AND item_id=:id AND telegram_id=:uid
                """),{"kind":kind,"id":item_id,"uid":telegram_id})
            await s.execute(text("""
              INSERT INTO admin_operation_audit(actor,action,target,details)
              VALUES(:actor,:action,:target,:note)
            """),{"actor":actor,"action":"issue_reviewed" if reviewed else "issue_reopened",
                  "target":kind+":"+item_id+":"+str(telegram_id),"note":note})
