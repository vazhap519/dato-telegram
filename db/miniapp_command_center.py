"""Read-only command centre for Telegram staff.

Reuse existing entitlements, payment registry and support tickets. Do not
conflate an active subscription with a confirmed paid event.
"""
from datetime import datetime,timezone
from sqlalchemy import text
from db.database import SessionLocal
from db.miniapp_analytics import TOTALS_SQL

SUPPORT_SQL="""
SELECT
 COUNT(*) FILTER(WHERE status='open') AS open,
 COUNT(*) FILTER(WHERE status='pending') AS pending,
 COUNT(*) FILTER(WHERE status='answered') AS answered,
 COUNT(*) FILTER(WHERE status<>'closed') AS unresolved
FROM support_tickets
"""
RECENT_SQL="""
SELECT t.id,t.status,t.category,t.updated_at,t.telegram_id,
       u.first_name,u.username
FROM support_tickets t LEFT JOIN users u ON u.telegram_id=t.telegram_id
WHERE t.status IN ('open','pending')
ORDER BY CASE WHEN t.status='open' THEN 0 ELSE 1 END,
         t.updated_at DESC,t.id DESC LIMIT 6
"""
PAYMENTS_SQL="""
SELECT COUNT(*) AS count
FROM verified_payment_receipts
WHERE payment_status='paid' AND verified_at>=NOW()-INTERVAL '30 days'
"""
EXPIRING_SQL="""
SELECT COUNT(*) AS count FROM (
 SELECT telegram_id FROM access_grants
 WHERE resource='community' AND revoked_at IS NULL
 LIMIT 0
) unused
"""

def iso(v):
    return v.isoformat() if isinstance(v,datetime) else None

async def overview(*,include_members:bool):
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("SET TRANSACTION READ ONLY, ISOLATION LEVEL REPEATABLE READ"))
            support=dict((await s.execute(text(SUPPORT_SQL))).mappings().one())
            recent=(await s.execute(text(RECENT_SQL))).mappings().all()
            members=None
            receipts=None
            if include_members:
                members=dict((await s.execute(text(TOTALS_SQL))).mappings().one())
                receipts=(await s.execute(text(PAYMENTS_SQL))).scalar_one()
    return {
        "as_of":datetime.now(timezone.utc).isoformat(),
        "support":support,
        "urgent_tickets":[{
            "id":r["id"],"status":r["status"],"category":r["category"],
            "updated_at":iso(r["updated_at"]),"telegram_id":r["telegram_id"],
            "display_name":r["first_name"] or r["username"] or f"ID {r['telegram_id']}",
        } for r in recent],
        "members":members,
        "paid_events_30d":receipts,
    }
