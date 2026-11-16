"""Opt-in Telegram notifications for the linked, enabled owner account.

An independent systemd timer calls this worker. It does NOT poll Telegram,
change subscriptions, or send to members. Every event has a DB unique key.
"""
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text
from db.database import engine,SessionLocal

log=logging.getLogger(__name__)
TZ=ZoneInfo("Asia/Tbilisi")
KINDS={"payments":"Новые оплаты","expiry":"Скорое окончание доступа",
       "attention":"Требуют внимания","digest":"Ежедневная сводка"}

async def initialize():
    """Explicit additive migration; invoke in a reviewed deployment step."""
    async with engine.begin() as db:
        await db.execute(text("""
          CREATE TABLE IF NOT EXISTS owner_alert_settings(
            admin_id INTEGER PRIMARY KEY REFERENCES admins(id) ON DELETE CASCADE,
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            payments BOOLEAN NOT NULL DEFAULT TRUE,
            expiry BOOLEAN NOT NULL DEFAULT TRUE,
            attention BOOLEAN NOT NULL DEFAULT TRUE,
            digest BOOLEAN NOT NULL DEFAULT TRUE,
            enabled_at TIMESTAMPTZ,
            payment_after_id BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
          )
        """))
        await db.execute(text("""
          CREATE TABLE IF NOT EXISTS owner_alert_deliveries(
            id BIGSERIAL PRIMARY KEY,
            admin_id INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
            event_key TEXT NOT NULL,
            event_kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'reserved',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            reserved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            sent_at TIMESTAMPTZ,
            UNIQUE(admin_id,event_key),
            CHECK (status IN ('reserved','sent','failed'))
          )
        """))
        await db.execute(text("""
          CREATE INDEX IF NOT EXISTS owner_alert_deliveries_state_idx
            ON owner_alert_deliveries(admin_id,status,reserved_at)
        """))

async def owner_settings(admin_id):
    async with SessionLocal() as db:
        row=(await db.execute(text("""
          SELECT a.id AS admin_id,a.username,a.role,a.enabled AS staff_enabled,
            l.telegram_id,COALESCE(n.enabled,FALSE) AS enabled,
            COALESCE(n.payments,TRUE) AS payments,
            COALESCE(n.expiry,TRUE) AS expiry,
            COALESCE(n.attention,TRUE) AS attention,
            COALESCE(n.digest,TRUE) AS digest,
            n.enabled_at
          FROM admins a LEFT JOIN support_telegram_links l ON l.admin_id=a.id
          LEFT JOIN owner_alert_settings n ON n.admin_id=a.id
          WHERE a.id=:id AND a.role='owner'
        """),{"id":admin_id})).mappings().first()
        return dict(row) if row else None

async def configure(admin_id,*,enabled,payments,expiry,attention,digest):
    """Owner explicitly opts in; starting point excludes historical payments."""
    async with SessionLocal() as db:
        async with db.begin():
            owner=(await db.execute(text("""
              SELECT a.id,l.telegram_id
              FROM admins a JOIN support_telegram_links l ON l.admin_id=a.id
              WHERE a.id=:id AND a.role='owner' AND a.enabled=TRUE FOR UPDATE OF a
            """),{"id":admin_id})).mappings().first()
            if not owner:raise ValueError("Сначала привяжите Telegram к действующей учётной записи владельца")
            previous=(await db.execute(text("""
              SELECT enabled FROM owner_alert_settings WHERE admin_id=:id
              FOR UPDATE
            """),{"id":admin_id})).scalar_one_or_none()
            # Never replay pre-opt-in payments; on re-enabling begin a new window.
            baseline=(await db.execute(text("SELECT COALESCE(MAX(id),0) FROM verified_payment_receipts"))).scalar_one()
            await db.execute(text("""
              INSERT INTO owner_alert_settings
                (admin_id,enabled,payments,expiry,attention,digest,enabled_at,payment_after_id)
              VALUES(:id,:on,:pay,:exp,:attention,:digest,
                CASE WHEN :on THEN NOW() ELSE NULL END,:baseline)
              ON CONFLICT(admin_id) DO UPDATE SET
                enabled=EXCLUDED.enabled,payments=EXCLUDED.payments,
                expiry=EXCLUDED.expiry,attention=EXCLUDED.attention,
                digest=EXCLUDED.digest,
                enabled_at=CASE WHEN NOT owner_alert_settings.enabled AND EXCLUDED.enabled
                                THEN NOW() ELSE owner_alert_settings.enabled_at END,
                payment_after_id=CASE WHEN NOT owner_alert_settings.enabled AND EXCLUDED.enabled
                                      THEN :baseline ELSE owner_alert_settings.payment_after_id END,
                updated_at=NOW()
            """),{"id":admin_id,"on":bool(enabled),"pay":bool(payments),
                  "exp":bool(expiry),"attention":bool(attention),
                  "digest":bool(digest),"baseline":baseline})
            # Do not create event deliveries for disabled settings.
    return await owner_settings(admin_id)

async def recipients():
    async with SessionLocal() as db:
        rows=(await db.execute(text("""
          SELECT n.admin_id,l.telegram_id,n.enabled_at,n.payment_after_id,
                 n.payments,n.expiry,n.attention,n.digest
          FROM owner_alert_settings n
          JOIN admins a ON a.id=n.admin_id
          JOIN support_telegram_links l ON l.admin_id=a.id
          WHERE n.enabled=TRUE AND a.enabled=TRUE AND a.role='owner'
            AND n.enabled_at IS NOT NULL
        """))).mappings().all()
    return [dict(row) for row in rows]

async def candidate_events(recipient,now=None):
    """No writes: examine only real receipts, effective expiration and recent reversals."""
    now=now or datetime.now(timezone.utc)
    day=now.astimezone(TZ).date().isoformat()
    events=[]
    async with SessionLocal() as db:
        if recipient["payments"]:
            payments=(await db.execute(text("""
              SELECT v.id,v.telegram_id,v.provider,v.amount,v.currency,v.verified_at
              FROM verified_payment_receipts v
              WHERE v.payment_status='paid'
                AND v.id>:cursor AND v.verified_at>=:start
              ORDER BY v.id LIMIT 50
            """),{"cursor":recipient["payment_after_id"],"start":recipient["enabled_at"]})).mappings().all()
            for p in payments:
                val=(str(p["amount"])+" "+(p["currency"] or "валюта не указана")) if p["amount"] is not None else "сумма не указана"
                events.append(("payments",f"payment:{p['id']}",
                    f"💳 Подтверждённая оплата\\nУчастник: {p['telegram_id']}\\n"
                    f"Система: {p['provider']}\\nСумма: {val}\\n"
                    "Откройте Mini App → Аналитика → Платежи."))
        # A person expires on the last surviving right, not on any one grant.
        # Rows exclude revoked and superseded entitlements and lifetime grants.
        if recipient["expiry"] or recipient["attention"]:
            from db.miniapp_analytics import ACCESS_CTE
            expr=ACCESS_CTE+"""
              SELECT telegram_id,username,first_name,expires_at
              FROM effective
              WHERE active AND NOT lifetime AND expires_at>NOW()
                AND expires_at<=NOW()+INTERVAL '3 days'
              ORDER BY expires_at,telegram_id LIMIT 100
            """
            due=(await db.execute(text(expr))).mappings().all()
            if recipient["expiry"]:
                for m in due:
                    delta=(m["expires_at"].date()-now.date()).days
                    if delta not in (0,1,3):continue
                    name=m["first_name"] or m["username"] or str(m["telegram_id"])
                    events.append(("expiry",f"expiry:{m['telegram_id']}:{m['expires_at'].isoformat()}:{delta}",
                        f"⏳ Скоро закончится доступ\\n{name} · ID {m['telegram_id']}\\n"
                        f"Срок: {m['expires_at'].astimezone(TZ).strftime('%d.%m.%Y')} (Тбилиси)\\n"
                        "Проверьте карточку участника."))
        if recipient["attention"]:
            # Reversals are genuine payment attention events; never re-notify past
            # transactions on activation. Payment records have no updated_at:
            # a reversed old payment cannot be dated reliably, so only recent
            # verified_at records are considered.
            reversals=(await db.execute(text("""
              SELECT id,telegram_id,provider,payment_status
              FROM verified_payment_receipts
              WHERE payment_status IN ('refunded','chargeback')
                AND verified_at>=:start
                AND verified_at>=NOW()-INTERVAL '2 days'
              ORDER BY id DESC LIMIT 50
            """),{"start":recipient["enabled_at"]})).mappings().all()
            for p in reversals:
                events.append(("attention",f"reversal:{p['id']}:{p['payment_status']}",
                    f"⚠️ Платёж требует внимания\\nID участника: {p['telegram_id']}\\n"
                    f"Система: {p['provider']}\\nСтатус: {p['payment_status']}"))
        if recipient["digest"] and now.astimezone(TZ).hour>=10:
            from db.miniapp_analytics import TOTALS_SQL,PAYMENT_COUNTS_SQL
            total=(await db.execute(text(TOTALS_SQL))).mappings().one()
            pay=(await db.execute(text(PAYMENT_COUNTS_SQL),{"period":"30d"})).mappings().one()
            events.append(("digest",f"digest:{day}",
                "📊 Ежедневная сводка · Круг Души\\n"
                f"Активный доступ: {total['active']}\\nБессрочный: {total['lifetime']}\\n"
                f"Активные подписки: {total['active_subscription_users']}\\n"
                f"Истекает за 7 дней: {total['expiring_7d']}\\n"
                f"Подтверждённых оплат за 30 дней: {pay['paid_events']}\\n"
                "Подробнее — Telegram Mini App → Аналитика."))
    return events

async def reserve(admin_id,event_key,event_kind):
    """Claim before sending, preventing duplicate sends by simultaneous timers."""
    async with SessionLocal() as db:
        async with db.begin():
            result=(await db.execute(text("""
              INSERT INTO owner_alert_deliveries(admin_id,event_key,event_kind,attempts)
              VALUES (:admin,:event,:kind,1)
              ON CONFLICT (admin_id,event_key) DO NOTHING
              RETURNING id
            """),{"admin":admin_id,"event":event_key,"kind":event_kind})).scalar_one_or_none()
    return result

async def finish(delivery_id,success,error=""):
    async with SessionLocal() as db:
        async with db.begin():
            await db.execute(text("""
              UPDATE owner_alert_deliveries
              SET status=:status,sent_at=CASE WHEN :ok THEN NOW() ELSE sent_at END,
                  last_error=:error
              WHERE id=:id
            """),{"id":delivery_id,"ok":success,"status":"sent" if success else "failed",
                  "error":error[:500] if error else None})

async def deliver(bot,*,dry_run=False):
    summary={"recipients":0,"candidates":0,"sent":0,"errors":0}
    for owner in await recipients():
        summary["recipients"]+=1
        events=await candidate_events(owner)
        summary["candidates"]+=len(events)
        if dry_run:continue
        for kind,key,message in events:
            claimed=await reserve(owner["admin_id"],key,kind)
            if claimed is None:continue
            try:
                await bot.send_message(owner["telegram_id"],message)
            except Exception as exc:
                log.exception("Owner alert send failure: %s",kind)
                await finish(claimed,False,str(exc))
                summary["errors"]+=1
            else:
                await finish(claimed,True)
                summary["sent"]+=1
    return summary
