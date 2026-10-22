"""Phase 1 subscription registry. No payment capture or Telegram member removal.

Access records are independent from payment processing. Manual grants are audited.
"""
from datetime import datetime, timezone
from sqlalchemy import text
from db.database import engine, SessionLocal

async def initialize_subscriptions():
    async with engine.begin() as db:
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS subscription_plans(
          id BIGSERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL,
          name TEXT NOT NULL, price_amount NUMERIC(12,2),
          currency TEXT NOT NULL DEFAULT 'EUR', duration_days INTEGER,
          is_lifetime BOOLEAN NOT NULL DEFAULT FALSE,
          is_active BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          CHECK(duration_days IS NULL OR duration_days > 0),
          CHECK(price_amount IS NULL OR price_amount >= 0)
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS subscriptions(
          id BIGSERIAL PRIMARY KEY,
          telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
          plan_id BIGINT REFERENCES subscription_plans(id),
          status TEXT NOT NULL DEFAULT 'pending',
          provider TEXT NOT NULL DEFAULT 'manual',
          provider_subscription_id TEXT,
          paid_until TIMESTAMPTZ,
          renews_automatically BOOLEAN NOT NULL DEFAULT FALSE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS access_grants(
          id BIGSERIAL PRIMARY KEY,
          telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
          resource TEXT NOT NULL,
          grant_type TEXT NOT NULL,
          starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          ends_at TIMESTAMPTZ,
          revoked_at TIMESTAMPTZ,
          reason TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          CHECK (resource IN ('community','library','materials')),
          CHECK (grant_type IN ('temporary','lifetime','gift','trial')),
          CHECK (ends_at IS NULL OR ends_at > starts_at)
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS subscription_audit_log(
          id BIGSERIAL PRIMARY KEY,
          telegram_id BIGINT,
          action TEXT NOT NULL,
          actor TEXT NOT NULL,
          details JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_orders(
          id BIGSERIAL PRIMARY KEY,
          telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
          plan_id BIGINT REFERENCES subscription_plans(id),
          provider TEXT NOT NULL,
          provider_order_id TEXT,
          status TEXT NOT NULL DEFAULT 'pending',
          amount NUMERIC(12,2), currency TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(provider,provider_order_id)
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_transactions(
          id BIGSERIAL PRIMARY KEY, order_id BIGINT REFERENCES payment_orders(id),
          provider TEXT NOT NULL, provider_transaction_id TEXT,
          kind TEXT NOT NULL, status TEXT NOT NULL,
          amount NUMERIC(12,2), currency TEXT,
          created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(provider,provider_transaction_id)
        )"""))
        await db.execute(text("""
        CREATE TABLE IF NOT EXISTS payment_webhook_events(
          id BIGSERIAL PRIMARY KEY, provider TEXT NOT NULL,
          event_key TEXT NOT NULL, signature_valid BOOLEAN NOT NULL,
          status TEXT NOT NULL DEFAULT 'received',
          payload JSONB NOT NULL, received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          UNIQUE(provider,event_key)
        )"""))
        await db.execute(text("CREATE INDEX IF NOT EXISTS idx_grants_user ON access_grants(telegram_id,resource)"))
        await db.execute(text("CREATE INDEX IF NOT EXISTS idx_subscriptions_user ON subscriptions(telegram_id)"))
        for code,name,price,days,life in [
          ('monthly','Месячная',None,30,False),
          ('yearly','Годовая',None,365,False),
          ('lifetime','Постоянный доступ',None,None,True),
          ('gift','Подарочный доступ',None,30,False),
          ('trial','Пробный доступ',None,7,False)
        ]:
            await db.execute(text("""
              INSERT INTO subscription_plans(code,name,price_amount,duration_days,is_lifetime)
              VALUES (:code,:name,:price,:days,:life)
              ON CONFLICT(code) DO NOTHING
            """),dict(code=code,name=name,price=price,days=days,life=life))

async def overview():
    async with SessionLocal() as s:
        summary=(await s.execute(text("""
          SELECT
           (SELECT COUNT(*) FROM users) users_total,
           (SELECT COUNT(DISTINCT telegram_id) FROM access_grants
             WHERE revoked_at IS NULL AND starts_at<=NOW()
               AND (ends_at IS NULL OR ends_at>NOW()) AND resource='community') community_access,
           (SELECT COUNT(DISTINCT telegram_id) FROM access_grants
             WHERE resource='community' AND revoked_at IS NULL AND ends_at IS NULL AND grant_type='lifetime') lifetime_access,
           (SELECT COUNT(DISTINCT telegram_id) FROM subscriptions
             WHERE status='active' AND paid_until>NOW()) recorded_active,
           (SELECT COUNT(DISTINCT telegram_id) FROM access_grants
             WHERE resource='community' AND revoked_at IS NULL AND ends_at BETWEEN NOW() AND NOW()+INTERVAL '7 days') expiring
        """))).mappings().one()
        plans=[dict(x) for x in (await s.execute(text(
          "SELECT * FROM subscription_plans ORDER BY id"
        ))).mappings()]
        return dict(summary),plans

async def people(search='', page=1):
    page=max(1,min(page,100000))
    async with SessionLocal() as s:
        params={"q":search.strip().lstrip("@")[:80],"offset":(page-1)*30}
        where=""" WHERE CAST(:q AS TEXT)='' OR CAST(u.telegram_id AS TEXT) ILIKE '%'||:q||'%'
            OR COALESCE(u.username,'') ILIKE '%'||:q||'%'
            OR COALESCE(u.first_name,'') ILIKE '%'||:q||'%'
            OR COALESCE(u.last_name,'') ILIKE '%'||:q||'%' """
        total=(await s.execute(text("SELECT count(*) FROM users u"+where),params)).scalar_one()
        rows=(await s.execute(text("""
          SELECT u.telegram_id,u.username,u.first_name,u.last_name,u.created_at,
          (SELECT COUNT(*) FROM access_grants g WHERE g.telegram_id=u.telegram_id
            AND g.resource='community' AND g.revoked_at IS NULL AND g.starts_at<=NOW()
            AND (g.ends_at IS NULL OR g.ends_at>NOW())) community_grants,
          (SELECT COUNT(*) FROM access_grants g WHERE g.telegram_id=u.telegram_id
            AND g.resource='community' AND g.grant_type='lifetime' AND g.revoked_at IS NULL) lifetime_grants
          FROM users u
        """+where+" ORDER BY u.created_at DESC,u.telegram_id DESC LIMIT 30 OFFSET :offset"),params)).mappings()
        return [dict(x) for x in rows],total

async def person(telegram_id):
    async with SessionLocal() as s:
        user=(await s.execute(text("SELECT * FROM users WHERE telegram_id=:uid"),{"uid":telegram_id})).mappings().first()
        if user is None: return None
        grants=[dict(x) for x in (await s.execute(text("""
          SELECT * FROM access_grants WHERE telegram_id=:uid AND resource='community' ORDER BY id DESC LIMIT 100
        """),{"uid":telegram_id})).mappings()]
        subs=[dict(x) for x in (await s.execute(text("""
          SELECT s.*,p.name plan_name FROM subscriptions s
          LEFT JOIN subscription_plans p ON p.id=s.plan_id
          WHERE s.telegram_id=:uid ORDER BY s.id DESC LIMIT 100
        """),{"uid":telegram_id})).mappings()]
        audit=[dict(x) for x in (await s.execute(text("""
          SELECT action,actor,details,created_at FROM subscription_audit_log
          WHERE telegram_id=:uid ORDER BY id DESC LIMIT 60
        """),{"uid":telegram_id})).mappings()]
        return dict(user),grants,subs,audit

async def grant_access(telegram_id,resource,grant_type,ends_at,reason,actor):
    if resource != 'community':
        raise ValueError('Неизвестный ресурс')
    if grant_type not in {'temporary','lifetime','gift','trial'}:
        raise ValueError('Неизвестный тип доступа')
    if grant_type=='lifetime':
        ends_at=None
    elif ends_at is None or ends_at <= datetime.now(timezone.utc):
        raise ValueError('Для временного доступа требуется будущая дата')
    if len(reason)>500: raise ValueError('Причина слишком длинная')
    async with SessionLocal() as s:
        async with s.begin():
            exists=(await s.execute(text("SELECT 1 FROM users WHERE telegram_id=:uid"),{"uid":telegram_id})).first()
            if not exists: raise ValueError('Пользователь ещё не запускал бота')
            grant_id=(await s.execute(text("""
              INSERT INTO access_grants(telegram_id,resource,grant_type,ends_at,reason,created_by)
              VALUES (:uid,:res,:kind,:end,:reason,:actor) RETURNING id
            """),{"uid":telegram_id,"res":resource,"kind":grant_type,"end":ends_at,"reason":reason,"actor":actor})).scalar_one()
            await s.execute(text("""
              INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
              VALUES (:uid,'grant_access',:actor,
              jsonb_build_object('grant_id',CAST(:id AS BIGINT),'resource',CAST(:res AS TEXT),'type',CAST(:kind AS TEXT),'reason',CAST(:reason AS TEXT)))
            """),{"uid":telegram_id,"actor":actor,"id":grant_id,"res":resource,"kind":grant_type,"reason":reason})

async def revoke_grant(telegram_id,grant_id,actor):
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
              UPDATE access_grants SET revoked_at=NOW()
              WHERE id=:gid AND telegram_id=:uid AND revoked_at IS NULL RETURNING resource,grant_type
            """),{"gid":grant_id,"uid":telegram_id})).first()
            if not row: raise ValueError('Право уже отозвано или не найдено')
            await s.execute(text("""
              INSERT INTO subscription_audit_log(telegram_id,action,actor,details)
              VALUES (:uid,'revoke_grant',:actor,jsonb_build_object('grant_id',CAST(:gid AS BIGINT)))
            """),{"uid":telegram_id,"gid":grant_id,"actor":actor})

async def update_plan(plan_id,price_amount,active):
    if price_amount is not None and (price_amount < 0 or price_amount > 1000000):
        raise ValueError('Неверная стоимость')
    async with SessionLocal() as s:
        async with s.begin():
            row=(await s.execute(text("""
              UPDATE subscription_plans SET price_amount=:price,is_active=:active
              WHERE id=:id RETURNING code
            """),{"id":plan_id,"price":price_amount,"active":active})).first()
            if not row: raise ValueError('Тариф не найден')
            await s.execute(text("""
              INSERT INTO subscription_audit_log(action,actor,details)
              VALUES ('plan_updated','admin',jsonb_build_object('plan_id',CAST(:id AS BIGINT),'price',CAST(:price AS NUMERIC),'active',CAST(:active AS BOOLEAN)))
            """),{"id":plan_id,"price":price_amount,"active":active})


async def create_plan(name, price_amount, currency, duration_days, is_lifetime, actor):
    """Create a custom, initially unpublished tariff. Existing plans are untouched."""
    import re
    from decimal import Decimal
    name = (name or '').strip()
    currency = (currency or '').strip().upper()
    if not 2 <= len(name) <= 100:
        raise ValueError('Название должно содержать от 2 до 100 символов')
    if currency not in {'EUR', 'USD', 'GEL'}:
        raise ValueError('Выберите EUR, USD или GEL')
    if price_amount is None or not price_amount.is_finite() or not (Decimal('0') <= price_amount <= Decimal('1000000')):
        raise ValueError('Укажите корректную цену')
    if price_amount.as_tuple().exponent < -2:
        raise ValueError('Цена допускает не более двух знаков после запятой')
    if not is_lifetime and (duration_days is None or not 1 <= duration_days <= 3650):
        raise ValueError('Срок тарифа должен быть от 1 до 3650 дней')
    if is_lifetime:
        duration_days = None
    import secrets
    code = 'custom_' + secrets.token_hex(9)
    async with SessionLocal() as s:
        async with s.begin():
            plan_id = (await s.execute(text("""
              INSERT INTO subscription_plans
              (code,name,price_amount,currency,duration_days,is_lifetime,is_active)
              VALUES (:code,:name,:price,:currency,:days,:life,FALSE)
              RETURNING id
            """), {'code':code,'name':name,'price':price_amount,'currency':currency,
                    'days':duration_days,'life':is_lifetime})).scalar_one()
            await s.execute(text("""
              INSERT INTO subscription_audit_log (action,actor,details)
              VALUES ('plan_created',:actor,
                jsonb_build_object('plan_id',CAST(:id AS BIGINT),'code',CAST(:code AS TEXT),'name',CAST(:name AS TEXT)))
            """), {'actor':actor,'id':plan_id,'code':code,'name':name})
    return plan_id
