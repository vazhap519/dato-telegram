"""Provider registry: configuration metadata, not payment verification.

Real payments require a separate authenticated provider-specific API/webhook
integration; toggling a provider here NEVER grants access to subscribers.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal

KNOWN = {"tribute": {"name":"Tribute","description":"Telegram-платежи · ожидается подключение Shop API"}}
STATES = {"planned":"Ожидает подключения","configured":"Настроен, без проверки","connected":"Подключён API"}
async def initialize_payment_providers():
    async with engine.begin() as c:
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS payment_provider_settings (
                code TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                public_url TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                integration_status TEXT NOT NULL DEFAULT 'planned',
                notes TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT NOT NULL DEFAULT 'system',
                CHECK (code ~ '^[a-z0-9_-]{2,40}$'),
                CHECK (integration_status IN ('planned','configured','connected'))
            )
        """))
        await c.execute(text("""
            INSERT INTO payment_provider_settings(code,display_name)
            VALUES ('tribute','Tribute') ON CONFLICT(code) DO NOTHING
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS payment_provider_audit (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """))

async def providers():
    async with SessionLocal() as s:
        return [dict(row) for row in (await s.execute(text("""
            SELECT code,display_name,public_url,enabled,integration_status,
                   notes,updated_at,updated_by
            FROM payment_provider_settings ORDER BY CASE WHEN code='tribute' THEN 0 ELSE 1 END,
            display_name
        """))).mappings().all()]

async def save_provider(code,public_url,notes,enabled,actor):
    from urllib.parse import urlparse
    if code not in KNOWN:
        raise ValueError("Новый провайдер требует отдельной интеграции API")
    public_url=(public_url or "").strip()
    if len(public_url)>1000 or len(notes or "")>500:
        raise ValueError("Слишком длинное значение")
    if public_url:
        u=urlparse(public_url)
        if u.scheme!="https" or not u.netloc or u.username or u.password:
            raise ValueError("Укажите корректную публичную HTTPS-ссылку")
    # Toggle is a display/config flag only, never a successful API connection.
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
                UPDATE payment_provider_settings SET
                  public_url=:url,notes=:notes,enabled=:enabled,
                  integration_status='planned',updated_at=NOW(),updated_by=:actor
                WHERE code=:code
            """),{"code":code,"url":public_url,"notes":(notes or "").strip(),
                  "enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO payment_provider_audit(code,actor,action)
                VALUES (:code,:actor,'settings_saved')
            """),{"code":code,"actor":actor})
