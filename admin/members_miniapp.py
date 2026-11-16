"""Read-only participant and subscription endpoints inside staff Telegram Mini App.

Uses the existing subscription registry and verified payment receipt ledger.
Access is checked on EVERY API call using signed Telegram initData and the
current DB role. Support-only operators are denied by default.
"""
from datetime import datetime
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import text

from admin.support_miniapp import require_staff
from db.database import SessionLocal
from db.subscriptions import people, person

router=APIRouter(prefix="/tg-support",tags=["telegram-members"])

def require_members_access(staff):
    if staff["role"] not in ("owner","admin"):
        raise HTTPException(403,"Нет доступа к участникам и подпискам")
    return staff

async def members_staff(staff=Depends(require_staff)):
    return require_members_access(staff)

def stamp(value):
    return value.isoformat() if isinstance(value,datetime) else None

@router.get("/members")
async def members_page():
    file=Path(__file__).resolve().parent/"templates"/"members_miniapp.html"
    return FileResponse(file,media_type="text/html",headers={
        "Cache-Control":"no-store",
        "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        "Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
    })

@router.get("/api/members")
async def members_list(q:str="", status:str="all", page:int=1,
                       staff=Depends(members_staff)):
    if len(q)>80 or page<1 or page>10000:
        raise HTTPException(400,"Недопустимые параметры поиска")
    if status not in ("all","active","lifetime","expiring","expired","none"):
        raise HTTPException(400,"Неверный статус")
    rows,total=await people(search=q,page=page,status=status)
    return {
        "total":total,"page":page,"page_size":30,
        "members":[{
            "telegram_id":r["telegram_id"],
            "username":r["username"],"first_name":r["first_name"],
            "last_name":r["last_name"],
            "access_status":r["access_status"],
            "active_access":bool(r["active_access"]),
            "is_lifetime":bool(r["is_lifetime"]),
            "expiry":stamp(r["expiry"]),
            "verified_payment_count":r["verified_payment_count"],
            "last_payment_at":stamp(r["last_payment_at"]),
            "last_payment_provider":r["last_payment_provider"],
        } for r in rows]
    }

@router.get("/api/members/{telegram_id}")
async def members_detail(telegram_id:int,staff=Depends(members_staff)):
    if telegram_id<=0:
        raise HTTPException(400,"Некорректный Telegram ID")
    result=await person(telegram_id)
    if not result:
        raise HTTPException(404,"Участник не найден")
    user,grants,subs,audit,selection=result
    async with SessionLocal() as session:
        receipts=(await session.execute(text("""
          SELECT provider,amount,currency,payment_status,verified_at
          FROM verified_payment_receipts WHERE telegram_id=:uid
          ORDER BY verified_at DESC,id DESC LIMIT 30
        """),{"uid":telegram_id})).mappings().all()
    return {
        "member":{
            "telegram_id":user["telegram_id"],
            "username":user.get("username"),
            "first_name":user.get("first_name"),
            "last_name":user.get("last_name"),
        },
        "grants":[{
            "resource":g["resource"],"grant_type":g["grant_type"],
            "starts_at":stamp(g["starts_at"]),"ends_at":stamp(g["ends_at"]),
            "revoked_at":stamp(g["revoked_at"])
        } for g in grants[:30]],
        "subscriptions":[{
            "plan":s["plan_name"],"status":s["status"],
            "provider":s["provider"],"paid_until":stamp(s["paid_until"]),
            "renews_automatically":s["renews_automatically"]
        } for s in subs[:30]],
        "payments":[{
            "provider":p["provider"],"amount":str(p["amount"]) if p["amount"] is not None else None,
            "currency":p["currency"],"status":p["payment_status"],
            "verified_at":stamp(p["verified_at"])
        } for p in receipts],
        "selected_plan":selection["plan_name"] if selection else None,
        "note":"Подтверждённый платёж и действующий доступ — разные показатели.",
    }
