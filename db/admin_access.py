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
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'owner'
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT TRUE
        """))
        await c.execute(text("""
          ALTER TABLE admins ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ
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
          SELECT id,username,role,enabled,last_login_at
          FROM admins WHERE username=:name
        """),{"name":username})).mappings().first()
        return dict(row) if row else None

async def staff_list():
    async with SessionLocal() as s:
        return [dict(x) for x in (await s.execute(text("""
          SELECT id,username,role,enabled,last_login_at
          FROM admins ORDER BY id
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
              INSERT INTO admins(username,password,role,enabled)
              VALUES (:name,:password,:role,TRUE)
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
              UPDATE admins SET role=:role,enabled=:enabled
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
