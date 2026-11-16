"""Opt-in lifecycle followups: backed by verified access events, existing broadcast queue.

The send operation is delegated to the already-running polling bot's existing
broadcast worker. This module never sends Telegram messages or starts polling.
No historical replay: activation time gates subscription audit events.
"""
from sqlalchemy import text
from db.database import engine, SessionLocal
from db.admin_extensions import audit as record_audit

KINDS={
 "welcome":"Приветствие после получения доступа",
 "renewal":"Сообщение после продления",
 "inactive":"Возвращение неактивных участников",
}
DEFAULTS={
 "welcome":"Здравствуйте, {first_name}! Добро пожаловать в «Круг Души». Если вы ещё не вошли в группу, откройте /subscription в нашем боте и запросите приглашение.",
 "renewal":"Здравствуйте, {first_name}! Ваш доступ к «Кругу Души» обновлён. Актуальный срок можно посмотреть через /subscription.",
 "inactive":"Здравствуйте, {first_name}! Давно не общались. Если хотите вернуться, загляните в «Круг Души».",
}
# No reliable cross-channel presence/reading signal currently exists. Inactivity
# remains hard-disabled, not inferred from absence of support messages/clicks.
SUPPORTED={"welcome","renewal"}
AUDIT_ACTIONS={
 "welcome":("grant_access","tribute_auto_paid","tribute_test_payment_activated"),
 "renewal":("manual_renewal",),
}

async def initialize_lifecycle():
    async with engine.begin() as conn:
        await conn.execute(text("""
          CREATE TABLE IF NOT EXISTS lifecycle_followup_rules(
            kind TEXT PRIMARY KEY CHECK(kind IN ('welcome','renewal','inactive')),
            enabled BOOLEAN NOT NULL DEFAULT FALSE,
            enabled_at TIMESTAMPTZ,
            body TEXT NOT NULL,
            updated_by TEXT NOT NULL DEFAULT 'system',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CHECK(enabled=FALSE OR kind <> 'inactive')
          )
        """))
        await conn.execute(text("""
          CREATE TABLE IF NOT EXISTS lifecycle_followup_deliveries(
            kind TEXT NOT NULL,
            source_audit_id BIGINT NOT NULL,
            telegram_id BIGINT NOT NULL REFERENCES users(telegram_id),
            broadcast_id BIGINT REFERENCES broadcasts(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY(kind,source_audit_id)
          )
        """))
        await conn.execute(text("""
          CREATE INDEX IF NOT EXISTS lifecycle_followup_user_idx
          ON lifecycle_followup_deliveries(telegram_id,created_at DESC)
        """))
        for kind,body in DEFAULTS.items():
            await conn.execute(text("""
              INSERT INTO lifecycle_followup_rules(kind,body)
              VALUES(:kind,:body) ON CONFLICT(kind) DO NOTHING
            """),{"kind":kind,"body":body})

async def rules():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT kind,enabled,enabled_at,body,updated_at,updated_by
          FROM lifecycle_followup_rules ORDER BY kind
        """))).mappings().all()
        return [{"kind":r["kind"],"title":KINDS[r["kind"]],
                 "enabled":r["enabled"],"enabled_at":r["enabled_at"].isoformat() if r["enabled_at"] else None,
                 "body":r["body"],"updated_at":r["updated_at"].isoformat(),
                 "supported":r["kind"] in SUPPORTED,
                 "note":"" if r["kind"] in SUPPORTED else
                 "Недостаточно достоверных данных об активности в группе; отправка заблокирована."}
                for r in rows]

async def set_lifecycle_rule(kind,enabled,body,actor):
    if kind not in KINDS:raise ValueError("Неизвестный сценарий")
    if enabled and kind not in SUPPORTED:
        raise ValueError("Возвращение неактивных недоступно до внедрения достоверного сигнала активности")
    body=(body or "").strip()
    if not 1<=len(body)<=3000:raise ValueError("Текст должен содержать от 1 до 3000 символов")
    allowed={"{first_name}"}
    if any(x not in allowed for x in __import__("re").findall(r"\{[^{}]+\}",body)):
        raise ValueError("Поддерживается только {first_name}")
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("""
              UPDATE lifecycle_followup_rules SET
                enabled=:enabled,body=:body,updated_by=:actor,updated_at=NOW(),
                enabled_at=CASE WHEN :enabled AND NOT enabled THEN NOW()
                                WHEN NOT :enabled THEN NULL ELSE enabled_at END
              WHERE kind=:kind
            """),{"enabled":bool(enabled),"body":body,"actor":actor,"kind":kind})
            # Disable queued, unprocessed recipients immediately. Already
            # in-flight deliveries cannot be unsent, acknowledged in UI.
            if not enabled:
                await s.execute(text("""
                  UPDATE broadcast_recipients r
                  SET status='skipped',error_code='lifecycle_disabled'
                  FROM broadcasts b
                  WHERE r.broadcast_id=b.id
                    AND b.campaign_kind=:campaign
                    AND r.status='pending'
                """),{"campaign":"lifecycle:"+kind})
    await record_audit(actor,"lifecycle_rule_updated",kind,"enabled" if enabled else "disabled")

PREVIEW_SQL="""
WITH events AS (
  SELECT a.id,a.telegram_id,a.action,a.created_at,
         ROW_NUMBER() OVER(PARTITION BY a.telegram_id ORDER BY a.id) AS seq
  FROM subscription_audit_log a
  JOIN lifecycle_followup_rules r ON r.kind=:kind
  WHERE r.enabled AND r.enabled_at IS NOT NULL
    AND a.created_at>=r.enabled_at AND a.created_at>=NOW()-INTERVAL '48 hours'
    AND (
      (:kind='welcome' AND a.action IN
        ('grant_access','tribute_auto_paid','tribute_test_payment_activated'))
      OR (:kind='renewal' AND a.action='manual_renewal')
    )
)
SELECT e.id,e.telegram_id,e.action,e.created_at
FROM events e
WHERE NOT EXISTS(
  SELECT 1 FROM lifecycle_followup_deliveries d
  WHERE d.kind=:kind AND d.source_audit_id=e.id
) AND NOT EXISTS(
  SELECT 1 FROM broadcast_opt_out o WHERE o.telegram_id=e.telegram_id
) AND (
  EXISTS(SELECT 1 FROM access_grants g
    WHERE g.telegram_id=e.telegram_id AND g.resource='community'
      AND g.revoked_at IS NULL AND g.starts_at<=NOW()
      AND (g.ends_at IS NULL OR g.ends_at>NOW()))
  OR EXISTS(SELECT 1 FROM subscriptions sub
    WHERE sub.telegram_id=e.telegram_id AND sub.status='active'
      AND sub.paid_until>NOW())
)
ORDER BY e.id LIMIT :limit
"""

async def dry_run(limit=20):
    result={}
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("SET TRANSACTION READ ONLY"))
            for kind in SUPPORTED:
                events=(await s.execute(text(PREVIEW_SQL),{"kind":kind,"limit":limit})).mappings().all()
                result[kind]={"eligible_now":len(events),
                              "sample_telegram_ids":[e["telegram_id"] for e in events[:5]]}
    result["inactive"]={"eligible_now":0,"blocked":"no_reliable_activity_signal"}
    return result

async def enqueue_lifecycle(limit=20,dry_run_only=True):
    """Separate explicit scheduler will call only after deployment approval.

    Each eligible audit event produces at most one existing broadcast campaign.
    Does not send messages or grant access. Requires installed migration.
    """
    if dry_run_only:return await dry_run(limit=limit)
    result={"welcome":0,"renewal":0}
    for kind in SUPPORTED:
        async with SessionLocal() as s:
            async with s.begin():
                # Serialize scanner calls, including simultaneous oneshots.
                await s.execute(text("SELECT pg_advisory_xact_lock(81200741, :lock_id)"),
                                {"lock_id":1 if kind=="welcome" else 2})
                events=(await s.execute(text(PREVIEW_SQL),{"kind":kind,"limit":limit})).mappings().all()
                rule=(await s.execute(text("""
                   SELECT enabled,body FROM lifecycle_followup_rules
                   WHERE kind=:kind FOR UPDATE
                """),{"kind":kind})).mappings().first()
                if not rule or not rule["enabled"]:continue
                for event in events:
                    # Outbox ledger and recipient transactionally committed,
                    # so restart cannot create a duplicate.
                    uid=event["telegram_id"]
                    record=(await s.execute(text("""
                      INSERT INTO lifecycle_followup_deliveries(kind,source_audit_id,telegram_id)
                      VALUES(:kind,:source,:uid)
                      ON CONFLICT DO NOTHING RETURNING source_audit_id
                    """),{"kind":kind,"source":event["id"],"uid":uid})).scalar_one_or_none()
                    if record is None:continue
                    bid=(await s.execute(text("""
                      INSERT INTO broadcasts(body,audience,campaign_kind,recipient_id,status)
                      VALUES(:body,'started',:campaign,:uid,'queued') RETURNING id
                    """),{"body":rule["body"],"campaign":"lifecycle:"+kind,"uid":uid})).scalar_one()
                    await s.execute(text("""
                      INSERT INTO broadcast_recipients(broadcast_id,telegram_id)
                      VALUES(:bid,:uid)
                    """),{"bid":bid,"uid":uid})
                    await s.execute(text("""
                      UPDATE lifecycle_followup_deliveries SET broadcast_id=:bid
                      WHERE kind=:kind AND source_audit_id=:source
                    """),{"bid":bid,"kind":kind,"source":event["id"]})
                    result[kind]+=1
    return result
