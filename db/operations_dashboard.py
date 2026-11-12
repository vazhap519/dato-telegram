"""Read-only, DB-backed health of existing automation pipelines."""
from sqlalchemy import text
from db.database import SessionLocal

async def automation_overview():
    async with SessionLocal() as s:
        rows=(await s.execute(text("""
          SELECT
          (SELECT notifications_enabled FROM subscription_automation_settings WHERE id=1) expiry_enabled,
          (SELECT removal_enabled FROM subscription_automation_settings WHERE id=1) removal_enabled,
          (SELECT COUNT(*) FROM subscription_expiry_notifications WHERE status='failed') expiry_failures,
          (SELECT MAX(attempted_at) FROM subscription_expiry_notifications) expiry_last_attempt,
          (SELECT COUNT(*) FROM payment_webhook_events
            WHERE provider='tribute' AND status IN ('pending_review','needs_attention')) tribute_attention,
          (SELECT MAX(received_at) FROM payment_webhook_events WHERE provider='tribute') last_tribute,
          (SELECT COUNT(*) FROM broadcasts WHERE status IN ('queued','sending')) queued_broadcasts,
          (SELECT MAX(created_at) FROM broadcasts) latest_broadcast,
          (SELECT COUNT(*) FROM broadcast_recipients WHERE status IN ('failed','uncertain')) failed_recipients,
          (SELECT COUNT(*) FROM crm_task_reminder_attempts WHERE state='failed') failed_crm_alerts,
          (SELECT COUNT(*) FROM crm_tasks WHERE status='open' AND due_at<=NOW()) overdue_tasks,
          (SELECT COUNT(*) FROM reminder_rules WHERE enabled) reminder_rules_enabled,
          (SELECT COUNT(*) FROM broadcasts b
            JOIN broadcast_recipients r ON r.broadcast_id=b.id
            WHERE b.campaign_kind LIKE 'reminder:%'
              AND r.status IN ('pending','processing','failed','uncertain')) reminder_problems
        """))).mappings().one()
        return dict(rows)

async def reminder_delivery_attention(limit=100):
    """Read actual broadcast recipient states; reminder_deliveries is only a deduplication ledger."""
    async with SessionLocal() as session:
        rows=(await session.execute(text("""
            SELECT b.id AS broadcast_id, b.campaign_kind, b.created_at,
                   r.telegram_id, r.status, r.error_code, r.sent_at,
                   u.first_name, u.username
            FROM broadcasts b
            JOIN broadcast_recipients r ON r.broadcast_id=b.id
            LEFT JOIN users u ON u.telegram_id=r.telegram_id
            WHERE b.campaign_kind LIKE 'reminder:%'
              AND r.status IN ('pending','processing','failed','uncertain')
            ORDER BY b.created_at DESC, b.id DESC
            LIMIT :limit
        """), {"limit":min(100,max(1,int(limit)))})).mappings().all()
        return [dict(row) for row in rows]
