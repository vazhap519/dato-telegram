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
            ALTER TABLE payment_provider_settings
            ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ
        """))
        await c.execute(text("""
            INSERT INTO payment_provider_settings(code,display_name)
            VALUES ('tribute','Tribute') ON CONFLICT(code) DO NOTHING
        """))
        await c.execute(text("""
            CREATE TABLE IF NOT EXISTS verified_payment_receipts (
                id BIGSERIAL PRIMARY KEY,
                provider TEXT NOT NULL REFERENCES payment_provider_settings(code),
                provider_payment_id TEXT NOT NULL,
                telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
                subscription_id BIGINT REFERENCES subscriptions(id),
                amount NUMERIC(12,2),
                currency TEXT,
                payment_status TEXT NOT NULL DEFAULT 'paid',
                verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source TEXT NOT NULL DEFAULT 'verified_webhook',
                UNIQUE(provider,provider_payment_id),
                CHECK (payment_status IN ('paid','refunded','chargeback'))
            )
        """))
        await c.execute(text("""
            CREATE INDEX IF NOT EXISTS verified_payment_receipts_user_idx
            ON verified_payment_receipts(telegram_id,verified_at DESC)
        """))
        await c.execute(text("""
            CREATE INDEX IF NOT EXISTS verified_payment_receipts_provider_idx
            ON verified_payment_receipts(provider,verified_at DESC)
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
            FROM payment_provider_settings WHERE archived_at IS NULL
            ORDER BY CASE WHEN code='tribute' THEN 0 ELSE 1 END,
            display_name
        """))).mappings().all()]

async def save_provider(code,public_url,notes,enabled,actor):
    from urllib.parse import urlparse
    async with SessionLocal() as s:
        known=(await s.execute(text(
            "SELECT 1 FROM payment_provider_settings WHERE code=:code AND archived_at IS NULL"
        ),{"code":code})).first()
    if not known:
        raise ValueError("Платёжная система не найдена")
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
                WHERE code=:code AND archived_at IS NULL
            """),{"code":code,"url":public_url,"notes":(notes or "").strip(),
                  "enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO payment_provider_audit(code,actor,action)
                VALUES (:code,:actor,'settings_saved')
            """),{"code":code,"actor":actor})


async def register_provider(code,name,actor):
    """Register future provider as inactive metadata, not a payment integration."""
    import re
    code=(code or "").strip().lower()
    name=(name or "").strip()
    if not re.fullmatch(r"[a-z0-9_-]{2,40}",code):
        raise ValueError("Код: 2–40 латинских букв, цифр, дефисов или подчёркиваний")
    if not 2<=len(name)<=80:
        raise ValueError("Название: от 2 до 80 символов")
    async with SessionLocal() as s:
        async with s.begin():
            exists=(await s.execute(text("""
                SELECT 1 FROM payment_provider_settings WHERE code=:code
            """),{"code":code})).first()
            if exists: raise ValueError("Провайдер с таким кодом уже существует")
            await s.execute(text("""
                INSERT INTO payment_provider_settings
                    (code,display_name,enabled,integration_status,updated_by)
                VALUES (:code,:name,FALSE,'planned',:actor)
            """),{"code":code,"name":name,"actor":actor})
            await s.execute(text("""
                INSERT INTO payment_provider_audit(code,actor,action)
                VALUES (:code,:actor,'registered')
            """),{"code":code,"actor":actor})


async def record_verified_payment(provider,payment_id,telegram_id,
                                  amount=None,currency=None,subscription_id=None):
    """Internal-only entry point. Call AFTER provider-specific signature and payment checks.

    No public unauthenticated endpoint should call this without verification.
    Same payment event is idempotent; manual grants do not become purchases.
    """
    if not provider or not payment_id or len(str(payment_id))>255:
        raise ValueError("Недопустимый идентификатор платежа")
    async with SessionLocal() as s:
        async with s.begin():
            provider_row=(await s.execute(text("""
                SELECT code FROM payment_provider_settings WHERE code=:provider
            """),{"provider":provider})).first()
            if not provider_row: raise ValueError("Неизвестный провайдер")
            user=(await s.execute(text("""
                SELECT 1 FROM users WHERE telegram_id=:uid
            """),{"uid":telegram_id})).first()
            if not user: raise ValueError("Неизвестный Telegram ID покупателя")
            result=(await s.execute(text("""
                INSERT INTO verified_payment_receipts
                  (provider,provider_payment_id,telegram_id,subscription_id,amount,currency)
                VALUES (:provider,:payment,:uid,:subscription,:amount,:currency)
                ON CONFLICT(provider,provider_payment_id) DO NOTHING
                RETURNING id
            """),{"provider":provider,"payment":str(payment_id),"uid":telegram_id,
                  "subscription":subscription_id,"amount":amount,"currency":currency})).scalar_one_or_none()
            if result is None:
                existing=(await s.execute(text("""
                    SELECT telegram_id FROM verified_payment_receipts
                    WHERE provider=:provider AND provider_payment_id=:payment
                """),{"provider":provider,"payment":str(payment_id)})).scalar_one()
                if existing!=telegram_id:
                    raise ValueError("Платёж уже привязан к другому покупателю")
            return result is not None


async def payment_provider_counts():
    async with SessionLocal() as s:
        return [dict(x) for x in (await s.execute(text("""
            SELECT p.code,p.display_name,
              COUNT(DISTINCT v.telegram_id) FILTER (WHERE v.payment_status='paid') buyers,
              COUNT(v.id) FILTER (WHERE v.payment_status='paid') payments
            FROM payment_provider_settings p
            LEFT JOIN verified_payment_receipts v ON v.provider=p.code
            GROUP BY p.code,p.display_name
            ORDER BY p.display_name
        """))).mappings().all()]


async def archive_provider(code,actor):
    """Hide provider without deleting historical transactions or payment attribution."""
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
                UPDATE payment_provider_settings
                SET archived_at=NOW(),enabled=FALSE,updated_at=NOW(),updated_by=:actor
                WHERE code=:code AND archived_at IS NULL
                RETURNING code
            """),{"code":code,"actor":actor})).first()
            if not row: raise ValueError("Платёжная система уже удалена или не существует")
            await s.execute(text("""
                INSERT INTO payment_provider_audit(code,actor,action)
                VALUES (:code,:actor,'archived')
            """),{"code":code,"actor":actor})


async def archived_providers():
    async with SessionLocal() as s:
        return [dict(row) for row in (await s.execute(text("""
            SELECT code,display_name,archived_at FROM payment_provider_settings
            WHERE archived_at IS NOT NULL ORDER BY archived_at DESC
        """))).mappings().all()]


async def restore_provider(code,actor):
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
                UPDATE payment_provider_settings
                SET archived_at=NULL,enabled=FALSE,updated_at=NOW(),updated_by=:actor
                WHERE code=:code AND archived_at IS NOT NULL RETURNING code
            """),{"code":code,"actor":actor})).first()
            if not row: raise ValueError("Система не найдена в архиве")
            await s.execute(text("""
                INSERT INTO payment_provider_audit(code,actor,action)
                VALUES (:code,:actor,'restored')
            """),{"code":code,"actor":actor})
