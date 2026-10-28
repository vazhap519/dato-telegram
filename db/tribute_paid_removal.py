"""Zero-grace removal of expired, *bot-managed* paid Tribute members only.

Strictly limited to actual group joins from our bot's one-use invitations and
signed, correctly bound production offer 279348.  A missing renewal means
paid_until is the end of access: NO extra seven days.  This module only
selects candidates; Telegram ban/unban happens in the existing worker after
two independent rechecks.  No rights or lifetime grants are ever revoked.
"""
from __future__ import annotations

from sqlalchemy import text

PRODUCTION_OFFER_ID = 279348


class TributeRemovalError(ValueError):
    pass


async def initialize_tribute_paid_removal() -> None:
    from db.database import engine
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(638492701)"))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tribute_paid_removal_settings (
                id INTEGER PRIMARY KEY CHECK (id=1),
                enabled BOOLEAN NOT NULL DEFAULT FALSE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_by TEXT NOT NULL DEFAULT 'system'
            )
        """))
        await conn.execute(text("""
            INSERT INTO tribute_paid_removal_settings(id) VALUES (1)
            ON CONFLICT (id) DO NOTHING
        """))


async def tribute_paid_removal_status() -> dict:
    """Show current safety switch and scope. Read-only."""
    from db.database import SessionLocal
    async with SessionLocal() as session:
        setting=(await session.execute(text("""
            SELECT enabled,updated_at,updated_by
            FROM tribute_paid_removal_settings WHERE id=1
        """))).mappings().one()
        total=(await session.execute(text("""
            SELECT COUNT(DISTINCT m.telegram_id)
            FROM subscription_managed_members m
            JOIN tribute_auto_buyers b
              ON b.telegram_id=m.telegram_id AND b.offer_id=:offer
            JOIN subscriptions s
              ON s.id=b.subscription_row_id AND s.telegram_id=m.telegram_id
            JOIN telegram_group_settings grp ON grp.id=1
            WHERE m.removed_at IS NULL AND m.chat_id=grp.chat_id
              AND s.provider='tribute'
        """),{"offer":PRODUCTION_OFFER_ID})).scalar_one()
    return {"enabled":bool(setting["enabled"]),
            "updated_at":setting["updated_at"],
            "updated_by":setting["updated_by"],
            "tracked_paid_members":total}


async def set_tribute_paid_removal_enabled(enabled: bool, actor: str):
    """Require explicitly enabling this narrow policy; never widen global removals."""
    from db.database import SessionLocal
    if type(enabled) is not bool or not isinstance(actor,str) or not 1<=len(actor)<=100:
        raise TributeRemovalError("Некорректные настройки исключения")
    async with SessionLocal() as s:
        async with s.begin():
            if enabled:
                paid=(await s.execute(text("""
                    SELECT enabled FROM tribute_auto_settings WHERE id=1
                """))).scalar_one_or_none()
                if paid is not True:
                    raise TributeRemovalError("Сначала включите автоматическую проверку оплат Tribute")
                global_removal=(await s.execute(text("""
                    SELECT removal_enabled
                    FROM subscription_automation_settings WHERE id=1
                """))).scalar_one_or_none()
                if global_removal:
                    raise TributeRemovalError(
                        "Общий механизм исключений должен оставаться выключенным"
                    )
                group=(await s.execute(text("""
                    SELECT chat_id FROM telegram_group_settings WHERE id=1
                """))).scalar_one_or_none()
                if not group:
                    raise TributeRemovalError("Закрытая группа не подключена")
                offer=(await s.execute(text("""
                    SELECT COUNT(*) FROM tribute_auto_offers
                    WHERE subscription_id=:offer AND enabled=TRUE
                """),{"offer":PRODUCTION_OFFER_ID})).scalar_one()
                if offer!=4:
                    raise TributeRemovalError(
                        "Четыре тарифных периода Tribute должны быть разрешены"
                    )
            await s.execute(text("""
                UPDATE tribute_paid_removal_settings
                SET enabled=:enabled,updated_by=:actor,updated_at=NOW()
                WHERE id=1
            """),{"enabled":enabled,"actor":actor})
            await s.execute(text("""
                INSERT INTO subscription_audit_log(action,actor,details)
                VALUES('tribute_paid_removal_toggled',:actor,
                    jsonb_build_object('enabled',CAST(:enabled AS BOOLEAN),
                                       'offer_id',CAST(:offer AS BIGINT),
                                       'grace_hours',0))
            """),{"actor":actor,"enabled":enabled,"offer":PRODUCTION_OFFER_ID})


async def eligible_expired_tribute_members(chat_id: int, limit: int=20) -> list[dict]:
    """Only known expired production buyers, with zero grace and NO other access.

    Never rely on a computed '30 days' anniversary; paid_until in the signed
    Tribute webhook is the only expiry used.  Even a SIGNED pending renewal
    stops removal until its payment can be processed/reconciled.

    The final removal worker independently checks membership and calls this
    function again immediately before the Telegram API removal.
    """
    if type(chat_id) is not int or chat_id>=0:
        return []
    from db.database import SessionLocal
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
            SELECT DISTINCT m.telegram_id,m.chat_id
            FROM subscription_managed_members m
            JOIN tribute_auto_buyers b
              ON b.telegram_id=m.telegram_id AND b.offer_id=:offer
            JOIN subscriptions s
              ON s.id=b.subscription_row_id AND s.telegram_id=m.telegram_id
            JOIN tribute_paid_removal_settings r ON r.id=1 AND r.enabled=TRUE
            JOIN tribute_auto_settings a ON a.id=1 AND a.enabled=TRUE
            JOIN subscription_automation_settings general
              ON general.id=1 AND general.removal_enabled=FALSE
            JOIN telegram_group_settings grp
              ON grp.id=1 AND grp.chat_id=m.chat_id
            WHERE m.chat_id=:chat AND m.removed_at IS NULL
              AND s.provider='tribute' AND s.status='active'
              AND s.paid_until IS NOT NULL AND s.paid_until <= NOW()
              AND NOT EXISTS (
                  SELECT 1 FROM access_grants g
                  WHERE g.telegram_id=m.telegram_id AND g.resource='community'
                    AND g.revoked_at IS NULL
                    AND (
                        g.starts_at>NOW()
                        OR (g.starts_at<=NOW()
                            AND (g.ends_at IS NULL OR g.ends_at>NOW()))
                    )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM subscriptions active_sub
                  WHERE active_sub.telegram_id=m.telegram_id
                    AND active_sub.status='active'
                    AND (
                        active_sub.paid_until IS NULL
                        OR active_sub.paid_until>NOW()
                    )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM payment_webhook_events e
                  WHERE e.provider='tribute' AND e.signature_valid=TRUE
                    AND e.status IN ('pending_review','needs_attention')
                    AND e.payload->>'name' IN (
                        'new_subscription','renewed_subscription'
                    )
                    AND e.payload->'details'->>'telegram_user_id'
                        =CAST(m.telegram_id AS TEXT)
                    AND e.payload->'details'->>'subscription_id'
                        =CAST(:offer AS TEXT)
              )
            ORDER BY m.telegram_id
            LIMIT :limit
        """),{"chat":chat_id,"offer":PRODUCTION_OFFER_ID,
              "limit":min(max(int(limit),1),1000)})).mappings().all()
    return [dict(row) for row in rows]
