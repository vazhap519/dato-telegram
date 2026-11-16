"""Mini App automation centre: view existing pipelines; edit existing opt-in reminders.

No new sender, cron, worker, or second notification ledger.
"""
from datetime import datetime
from sqlalchemy import text
from db.database import SessionLocal
from db.automation import TRIGGERS, rules as existing_rules, set_rule
from db.operations_dashboard import automation_overview,reminder_delivery_attention
from db.subscription_automation import automation_settings,save_automation_settings
from db.admin_extensions import audit as record_audit

def stamp(v):return v.isoformat() if isinstance(v,datetime) else None

async def dashboard():
    # Existing pipeline counts and actual broadcast recipient states, not
    # just dedup marker status. No recipient IDs in the overview.
    summary=await automation_overview()
    expiry=await automation_settings()
    all_rules=await existing_rules()
    attention=await reminder_delivery_attention(limit=25)
    async with SessionLocal() as s:
        async with s.begin():
            await s.execute(text("SET TRANSACTION READ ONLY"))
            recent=(await s.execute(text("""
                SELECT b.id,b.campaign_kind,b.status,b.created_at,
                       COUNT(r.telegram_id) AS recipients,
                       COUNT(*) FILTER(WHERE r.status='sent') AS sent,
                       COUNT(*) FILTER(WHERE r.status IN ('failed','uncertain')) AS problems
                FROM broadcasts b
                LEFT JOIN broadcast_recipients r ON r.broadcast_id=b.id
                GROUP BY b.id ORDER BY b.id DESC LIMIT 12
            """))).mappings().all()
    return {
        "summary":{k:stamp(v) if isinstance(v,datetime) else v for k,v in summary.items()},
        "expiry":{"enabled":expiry["notifications_enabled"],
                  "removal_enabled":expiry["removal_enabled"],
                  "grace_hours":expiry["grace_hours"],
                  "updated_at":stamp(expiry["updated_at"])},
        "rules":[{"trigger":r["trigger"],"title":TRIGGERS.get(r["trigger"],r["trigger"]),
                  "enabled":r["enabled"],"body":r["body"]} for r in all_rules if r["trigger"] in TRIGGERS],
        "problems":[{"broadcast_id":r["broadcast_id"],
                     "trigger":r["campaign_kind"].removeprefix("reminder:"),
                     "status":r["status"],"error_code":r["error_code"],
                     "created_at":stamp(r["created_at"])} for r in attention],
        "recent":[{"id":r["id"],"kind":r["campaign_kind"],"status":r["status"],
                   "created_at":stamp(r["created_at"]),"recipients":r["recipients"],
                   "sent":r["sent"],"problems":r["problems"]} for r in recent],
        "planned":[
            {"name":"Приветствие после получения доступа","state":"not_implemented"},
            {"name":"Персональное сопровождение после продления","state":"not_implemented"},
            {"name":"Возвращение неактивных участников","state":"not_implemented"},
        ],
        "notes":[
            "Напоминания об оформлении через /start — существующие сценарии; некоторые уже могут быть включены.",
            "Напоминания об окончании доступа работают через отдельный существующий механизм.",
            "Новые сценарии пока не включены и не отправляют сообщений.",
            "Отправки учитывают отказы от рассылок и проверяют актуальность условия перед доставкой.",
        ],
    }

async def update_existing_rule(trigger,enabled,body,actor):
    if trigger not in TRIGGERS:raise ValueError("Неизвестный сценарий")
    await set_rule(trigger,bool(enabled),body)
    await record_audit(actor,"miniapp_reminder_rule_saved",trigger,"enabled" if enabled else "disabled")

async def update_expiry_notifications(enabled,actor):
    settings=await automation_settings()
    # Keep removal setting and grace period exactly as they were.
    await save_automation_settings(bool(enabled),settings["removal_enabled"],
                                   settings["grace_hours"],"telegram:"+actor)
