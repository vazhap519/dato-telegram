"""Member directory with signed Telegram authorization and audited owner actions.

Reuse the existing entitlement and verified-payment registries; never mark
manual access as a paid subscription. Read: owner/admin. Writes: owner only.
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse
from sqlalchemy import text

from admin.support_miniapp import require_staff
from db.database import SessionLocal
from db.miniapp_analytics import report as miniapp_analytics_report
from db.owner_alerts import owner_settings,configure as configure_owner_alerts
from db.subscriptions import (people, person, grant_access, revoke_grant,
                              change_grant_expiry, extend_community_access)

log=logging.getLogger(__name__)
TBILISI=ZoneInfo("Asia/Tbilisi")

router=APIRouter(prefix="/tg-support",tags=["telegram-members"])

def require_members_access(staff):
    if staff["role"] not in ("owner","admin"):
        raise HTTPException(403,"Нет доступа к участникам и подпискам")
    return staff

async def members_staff(staff=Depends(require_staff)):
    return require_members_access(staff)

def stamp(value):
    return value.isoformat() if isinstance(value,datetime) else None

def require_members_write(staff):
    # Operations that change entitlements are owner-only, irrespective of
    # staff's support reply/status/delete privileges.
    if staff["role"]!="owner":
        raise HTTPException(403,"Изменять доступ может только владелец")
    return staff

async def members_owner(staff=Depends(members_staff)):
    return require_members_write(staff)

def aware(value):
    if value is None:return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def grant_state(grant,now=None):
    now=now or datetime.now(timezone.utc)
    if grant["revoked_at"] is not None:return "revoked"
    start=aware(grant["starts_at"])
    end=aware(grant["ends_at"])
    if start>now:return "scheduled"
    if end is not None and end<=now:return "expired"
    return "active"

def describe_access(grants,subs,now=None):
    """Derive effective status without mistaking revoked grants for rights."""
    now=now or datetime.now(timezone.utc)
    active=[g for g in grants if grant_state(g,now)=="active"]
    lifetime=any(g["ends_at"] is None for g in active)
    paid=[s for s in subs if s["status"]=="active"
          and aware(s["paid_until"]) is not None and aware(s["paid_until"])>now]
    paid_to=max((aware(s["paid_until"]) for s in paid),default=None)
    grant_to=max((aware(g["ends_at"]) for g in active if g["ends_at"] is not None),default=None)
    expiry=max((d for d in (paid_to,grant_to) if d is not None),default=None)
    future=any(grant_state(g,now)=="scheduled" for g in grants)
    # Revoked future-dated rights are history, NOT an expired entitlement.
    historic=[aware(g["ends_at"]) for g in grants
              if g["revoked_at"] is None and g["ends_at"] is not None
              and aware(g["ends_at"])<=now]
    historic += [aware(s["paid_until"]) for s in subs
                 if s["status"]=="active" and s["paid_until"] is not None
                 and aware(s["paid_until"])<=now]
    if lifetime:
        status="lifetime"
    elif active or paid:
        status="expiring" if expiry and expiry<=now+timedelta(days=7) else "active"
    elif future:
        status="scheduled"
    elif historic:
        status="expired"
    else:
        status="none"
    if active and paid:
        basis="Ручной доступ и оплаченная подписка"
    elif active:
        basis="Ручной бессрочный доступ" if lifetime else "Ручной временный доступ"
    elif paid:
        basis="Подписка"
    elif future:
        basis="Доступ запланирован"
    else:
        basis="Нет действующего доступа"
    return {"status":status,"active":bool(active or paid),"basis":basis,
            "ends_at":None if lifetime else stamp(expiry),
            "lifetime":lifetime,"manual_count":len(active),
            "paid_subscription_count":len(paid)}

def date_end(local_date):
    try:
        date=datetime.strptime(local_date,"%Y-%m-%d")
        end=date.replace(hour=23,minute=59,second=59,tzinfo=TBILISI).astimezone(timezone.utc)
    except (TypeError,ValueError):
        raise HTTPException(400,"Укажите дату в формате ГГГГ-ММ-ДД")
    if end<=datetime.now(timezone.utc):
        raise HTTPException(400,"Дата окончания должна быть в будущем")
    return end

class GrantPayload(BaseModel):
    kind: str
    until: str | None = None
    reason: str = Field(default="",max_length=500)
    confirm: str

class ExtendPayload(BaseModel):
    days: int = Field(ge=1,le=3650)
    reason: str = Field(default="",max_length=500)
    confirm: str

class ExpiryPayload(BaseModel):
    until: str
    confirm: str

class RevokePayload(BaseModel):
    confirm: str

class OwnerAlertSettingsPayload(BaseModel):
    enabled: bool
    payments: bool = True
    expiry: bool = True
    attention: bool = True
    digest: bool = True

@router.get("/api/owner-alerts")
async def owner_alert_settings(staff=Depends(members_owner)):
    current=await owner_settings(staff["id"])
    if not current:
        raise HTTPException(403,"Доступ только владельцу")
    return {"enabled":current["enabled"],"payments":current["payments"],
            "expiry":current["expiry"],"attention":current["attention"],
            "digest":current["digest"],
            "telegram_linked":bool(current["telegram_id"]),
            "enabled_at":stamp(current["enabled_at"])}

@router.post("/api/owner-alerts")
async def update_owner_alert_settings(payload:OwnerAlertSettingsPayload,staff=Depends(members_owner)):
    try:
        current=await configure_owner_alerts(staff["id"],enabled=payload.enabled,
                payments=payload.payments,expiry=payload.expiry,
                attention=payload.attention,digest=payload.digest)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    return {"ok":True,"enabled":current["enabled"]}

@router.get("/analytics")
async def analytics_page():
    file=Path(__file__).resolve().parent/"templates"/"analytics_miniapp.html"
    return FileResponse(file,media_type="text/html",headers={
        "Cache-Control":"no-store",
        "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        "Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
    })

@router.get("/api/analytics")
async def analytics_data(period:str="30d",staff=Depends(members_staff)):
    if period not in ("30d","all"):
        raise HTTPException(400,"Недопустимый период")
    return await miniapp_analytics_report(period)

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
    # Derive the basis from actual current entitlements, not from payments
    # alone. Fetch only the current page (at most 30 participants).
    ids=[r["telegram_id"] for r in rows]
    detail={}
    if ids:
        async with SessionLocal() as session:
            grants=(await session.execute(text("""
              SELECT telegram_id,grant_type,starts_at,ends_at,revoked_at
              FROM access_grants
              WHERE resource='community' AND telegram_id=ANY(:ids)
                AND revoked_at IS NULL
                AND (ends_at IS NULL OR ends_at>NOW())
            """),{"ids":ids})).mappings().all()
            subs=(await session.execute(text("""
              SELECT telegram_id,status,paid_until
              FROM subscriptions
              WHERE telegram_id=ANY(:ids) AND status='active'
                AND paid_until>NOW()
            """),{"ids":ids})).mappings().all()
        for g in grants:
            detail.setdefault(g["telegram_id"],{"grants":[],"subs":[]})["grants"].append(g)
        for p in subs:
            detail.setdefault(p["telegram_id"],{"grants":[],"subs":[]})["subs"].append(p)
    basis={}
    for r in rows:
        d=detail.get(r["telegram_id"],{"grants":[],"subs":[]})
        basis[r["telegram_id"]]=describe_access(d["grants"],d["subs"])
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
            "access_basis":basis[r["telegram_id"]]["basis"],
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
        payment_count=(await session.execute(text("""
          SELECT COUNT(*) FROM verified_payment_receipts
          WHERE telegram_id=:uid AND payment_status='paid'
        """),{"uid":telegram_id})).scalar_one()
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
        "access":describe_access(grants,subs),
        "can_manage":staff["role"]=="owner",
        "verified_payment_count":payment_count,
        "grants":[{
            "id":g["id"],"state":grant_state(g),
            "resource":g["resource"],"grant_type":g["grant_type"],
            "starts_at":stamp(g["starts_at"]),"ends_at":stamp(g["ends_at"]),
            "revoked_at":stamp(g["revoked_at"])
        } for g in grants[:100]],
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
        "note":"Ручной доступ и подписка учитываются отдельно. Платёж подтверждён только при наличии записи в реестре платежей.",
    }


def require_confirm(received,expected):
    if received!=expected:
        raise HTTPException(400,"Подтвердите действие")

async def refresh_member(telegram_id):
    data=await person(telegram_id)
    if not data:raise HTTPException(404,"Участник не найден")
    return data

@router.post("/api/members/{telegram_id}/grant")
async def grant_member(telegram_id:int,payload:GrantPayload,staff=Depends(members_owner)):
    require_confirm(payload.confirm,"GRANT")
    if payload.kind not in ("temporary","lifetime"):
        raise HTTPException(400,"Выберите временный или бессрочный доступ")
    until=None if payload.kind=="lifetime" else date_end(payload.until)
    try:
        await grant_access(telegram_id,"community",payload.kind,until,payload.reason.strip(),
                           "telegram:"+staff["username"])
    except ValueError as exc:raise HTTPException(400,str(exc))
    # Keep invitation semantics consistent with the existing web admin.
    # Failure to invite cannot roll back the already-audited entitlement.
    invited="not_sent"
    try:
        import os
        from aiogram import Bot
        from bot.group_handlers import send_group_invitation
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            invited=await send_group_invitation(bot,telegram_id,staff["username"])
    except Exception:
        log.exception("Mini App invitation failed for user %s",telegram_id)
        invited="failed"
    return {"ok":True,"invitation":invited}

@router.post("/api/members/{telegram_id}/extend")
async def extend_member(telegram_id:int,payload:ExtendPayload,staff=Depends(members_owner)):
    require_confirm(payload.confirm,"EXTEND")
    try:
        expiry=await extend_community_access(telegram_id,payload.days,payload.reason.strip(),
                                              "telegram:"+staff["username"])
    except ValueError as exc:raise HTTPException(400,str(exc))
    # Notification happens after DB commit; failure is reported, not rolled back.
    notice="not_sent"
    try:
        from admin.renewal_notice import notify_renewal
        notice=await notify_renewal(telegram_id,expiry,staff["username"])
    except Exception:
        log.exception("Mini App extension notice failed for %s",telegram_id)
        notice="failed"
    return {"ok":True,"expires_at":stamp(expiry),"notification":notice}

@router.post("/api/members/{telegram_id}/grants/{grant_id}/expiry")
async def edit_member_grant(telegram_id:int,grant_id:int,payload:ExpiryPayload,
                            staff=Depends(members_owner)):
    require_confirm(payload.confirm,"CHANGE_EXPIRY")
    try:
        await change_grant_expiry(telegram_id,grant_id,date_end(payload.until),
                                  "telegram:"+staff["username"])
    except ValueError as exc:raise HTTPException(400,str(exc))
    return {"ok":True}

@router.post("/api/members/{telegram_id}/grants/{grant_id}/revoke")
async def revoke_member_grant(telegram_id:int,grant_id:int,payload:RevokePayload,
                              staff=Depends(members_owner)):
    require_confirm(payload.confirm,"REVOKE")
    try:
        await revoke_grant(telegram_id,grant_id,"telegram:"+staff["username"])
    except ValueError as exc:raise HTTPException(400,str(exc))
    removal="not_attempted"
    try:
        from admin.revocation_removal import remove_after_revoke
        removal=await remove_after_revoke(telegram_id)
    except Exception:
        log.exception("Membership removal check failed for %s",telegram_id)
        removal="failed"
    return {"ok":True,"group_removal":removal}
