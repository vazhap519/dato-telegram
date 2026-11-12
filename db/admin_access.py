"""Server-enforced staff roles for the admin app, with legacy owner bootstrap."""
from sqlalchemy import text
from db.database import engine,SessionLocal

ROLES={"owner","admin","support","analyst"}

async def initialize_staff():
    async with engine.begin() as c:
        await c.execute(text("""
          CREATE TABLE IF NOT EXISTS admins(
            id SERIAL PRIMARY KEY,username TEXT UNIQUE,password TEXT
          )
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'admin'
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE
        """))
        for column in ("support_access", "support_can_reply", "support_can_change_status",
                       "support_can_close", "support_can_delete"):
            await c.execute(text(
                f"ALTER TABLE admins ADD COLUMN IF NOT EXISTS {column} BOOLEAN NOT NULL DEFAULT TRUE"
            ))
        # Existing oldest admin account is the initial owner. Other legacy
        # accounts remain admins rather than gaining owner-only privileges.
        await c.execute(text("""
          UPDATE admins SET role='owner'
          WHERE id=(SELECT MIN(id) FROM admins)
            AND NOT EXISTS(SELECT 1 FROM admins WHERE role='owner')
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0
        """))
        await c.execute(text("""
          CREATE TABLE IF NOT EXISTS admin_auth_events(
            id BIGSERIAL PRIMARY KEY,username TEXT NOT NULL,
            outcome TEXT NOT NULL,ip TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
          )
        """))
        await c.execute(text("""
          CREATE INDEX IF NOT EXISTS admin_auth_event_lookup
          ON admin_auth_events(username,created_at DESC)
        """))

def allowed(role,path,method):
    if role=="owner":
        return True
    if role=="admin":
        return path!="/staff" and not path.startswith("/staff/") and path!="/change-credentials"
    if role=="support":
        return path=="/support" or path.startswith("/support/")
    if role=="analyst":
        return method=="GET" and path in ("/dashboard","/analytics","/events","/search")
    return False

async def read_staff(username):
    async with SessionLocal() as s:
        row=(await s.execute(text("""
          SELECT id,username,role,enabled,last_login_at,session_version,
                 support_access,support_can_reply,support_can_change_status,
                 support_can_close,support_can_delete
          FROM admins WHERE username=:name
        """),{"name":username})).mappings().first()
        return dict(row) if row else None

async def staff_list():
    async with SessionLocal() as s:
        return [dict(x) for x in (await s.execute(text("""
          SELECT a.id,a.username,a.role,a.enabled,a.last_login_at,
                 a.support_access,a.support_can_reply,a.support_can_change_status,
                 a.support_can_close,a.support_can_delete,
                 l.telegram_id,COALESCE(l.notifications_enabled,FALSE) AS support_notify_telegram
          FROM admins a LEFT JOIN support_telegram_links l ON l.admin_id=a.id
          ORDER BY a.id
        """))).mappings().all()]

async def staff_create(username,password_hash,role,actor):
    username=(username or "").strip()
    if len(username)<3 or len(username)>60 or not username.replace("_","").isalnum():
        raise ValueError("Имя: 3–60 букв, цифр или подчёркиваний")
    if role not in ROLES or role=="owner":
        raise ValueError("Нельзя выдавать роль владельца через эту форму")
    async with SessionLocal() as s:
        async with s.begin():
            exists=(await s.execute(text("""
              SELECT 1 FROM admins WHERE username=:name
            """),{"name":username})).first()
            if exists:raise ValueError("Учётная запись уже существует")
            await s.execute(text("""
              INSERT INTO admins(username,password,role,enabled,
                support_access,support_can_reply,support_can_change_status,
                support_can_close,support_can_delete)
              VALUES (:name,:password,:role,TRUE,FALSE,FALSE,FALSE,FALSE,FALSE)
            """),{"name":username,"password":password_hash,"role":role})
            await s.execute(text("""
              INSERT INTO admin_operation_audit(actor,action,target,details)
              VALUES (:actor,'staff_created',:target,:role)
            """),{"actor":actor,"target":username,"role":role})

async def staff_change(target,role,enabled,actor):
    if role not in ROLES or role=="owner":
        raise ValueError("Роль владельца нельзя назначать здесь")
    if target==actor:
        raise ValueError("Нельзя изменить собственные права")
    async with SessionLocal() as s:
        async with s.begin():
            updated=(await s.execute(text("""
              UPDATE admins SET role=:role,enabled=:enabled,
                session_version=session_version+1
              WHERE username=:target AND role<>'owner' RETURNING id
            """),{"target":target,"role":role,"enabled":enabled})).first()
            if not updated:raise ValueError("Сотрудник не найден или владелец защищён")
            await s.execute(text("""
              INSERT INTO admin_operation_audit(actor,action,target,details)
              VALUES (:actor,'staff_access_changed',:target,:details)
            """),{"actor":actor,"target":target,"details":role+" / "+("enabled" if enabled else "disabled")})

async def record_login(username,outcome,ip):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
              INSERT INTO admin_auth_events(username,outcome,ip) VALUES (:u,:o,:ip)
            """),{"u":(username or "")[:60],"o":outcome,"ip":(ip or "")[:80]})

async def login_throttled(username):
    async with SessionLocal() as s:
        count=(await s.execute(text("""
          SELECT COUNT(*) FROM admin_auth_events
          WHERE username=:u AND outcome='failed'
            AND created_at>NOW()-INTERVAL '15 minutes'
        """),{"u":username[:60]})).scalar_one()
        return count>=8

async def mark_logged_in(username):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
              UPDATE admins SET last_login_at=NOW() WHERE username=:u
            """),{"u":username})


async def recent_login_events(limit=80):
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT username,outcome,ip,created_at
          FROM admin_auth_events ORDER BY id DESC LIMIT :limit
        """),{"limit":min(200,max(1,int(limit)))})).mappings().all()
        return [dict(row) for row in rows]

async def support_permissions_change(target, actor, *, access=False, can_reply=False,
                                     can_change_status=False, can_close=False,
                                     can_delete=False, telegram_id=None, notify=False):
    """Owner-only caller manages support rights and optional Telegram pairing.

    This is separate from the web account role; owner role is immutable.
    Revocation takes effect on the next Mini App request.
    """
    if target == actor:
        raise ValueError("Нельзя изменять собственные права поддержки в этой форме")
    if telegram_id not in (None, ""):
        try:
            telegram_id = int(telegram_id)
        except (TypeError, ValueError):
            raise ValueError("Неверный Telegram ID")
        if telegram_id <= 0 or telegram_id >= 10**16:
            raise ValueError("Неверный Telegram ID")
    else:
        telegram_id = None
    async with SessionLocal() as s:
        async with s.begin():
            account=(await s.execute(text("""
                SELECT id,role,enabled FROM admins WHERE username=:name FOR UPDATE
            """),{"name":target})).mappings().first()
            if not account or account["role"]=="owner":
                raise ValueError("Сотрудник не найден или аккаунт владельца защищён")
            access=bool(access) and account["role"] in ("admin","support")
            rights={"access":access, "reply":access and bool(can_reply),
                    "status":access and bool(can_change_status),
                    "close":access and bool(can_close),
                    "delete":access and bool(can_delete)}
            await s.execute(text("""
                UPDATE admins SET support_access=:access, support_can_reply=:reply,
                    support_can_change_status=:status, support_can_close=:close,
                    support_can_delete=:delete, session_version=session_version+1
                WHERE id=:id
            """),{"id":account["id"],**rights})
            if telegram_id is not None:
                taken=(await s.execute(text("""
                    SELECT 1 FROM support_telegram_links
                    WHERE telegram_id=:tid AND admin_id<>:id
                """),{"tid":telegram_id,"id":account["id"]})).first()
                if taken:
                    raise ValueError("Этот Telegram ID уже привязан к другому сотруднику")
                await s.execute(text("""
                    INSERT INTO support_telegram_links
                        (admin_id,telegram_id,notifications_enabled,notifications_since,reply_ticket_id)
                    VALUES (:id,:tid,:notify,
                        CASE WHEN :notify THEN NOW() ELSE NULL END,NULL)
                    ON CONFLICT(admin_id) DO UPDATE SET telegram_id=EXCLUDED.telegram_id,
                        notifications_enabled=EXCLUDED.notifications_enabled,
                        notifications_since=CASE WHEN EXCLUDED.notifications_enabled
                            THEN NOW() ELSE NULL END,
                        reply_ticket_id=NULL,updated_at=NOW()
                """),{"id":account["id"],"tid":telegram_id,
                      "notify":access and account["enabled"] and bool(notify)})
            else:
                await s.execute(text("""
                    UPDATE support_telegram_links
                    SET notifications_enabled=:notify,
                        notifications_since=CASE WHEN :notify THEN
                            COALESCE(notifications_since,NOW()) ELSE NULL END,
                        reply_ticket_id=CASE WHEN :access THEN reply_ticket_id ELSE NULL END,
                        updated_at=NOW()
                    WHERE admin_id=:id
                """),{"id":account["id"],
                      "notify":access and account["enabled"] and bool(notify),
                      "access":access})
            await s.execute(text("""
                INSERT INTO admin_operation_audit(actor,action,target,details)
                VALUES (:actor,'support_permissions_changed',:target,:details)
            """),{"actor":actor,"target":target,
                  "details":"access="+str(access)+", notify="+str(bool(notify))})
