"""Read-only analyst Telegram Mini App. No web-admin password/session is used."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text

from db.database import SessionLocal
from db.admin_analytics import report as analytics_report

router=APIRouter(prefix="/tg-support/analyst",tags=["analyst-miniapp"])

async def require_analyst(x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data or len(x_telegram_init_data)>8192:
        raise HTTPException(401,"Откройте панель из Telegram")
    try:
        fields=dict(parse_qsl(x_telegram_init_data,keep_blank_values=True,strict_parsing=True))
        sent_hash=fields.pop("hash")
        if len(sent_hash)!=64 or not all(ch in "0123456789abcdefABCDEF" for ch in sent_hash):
            raise ValueError("hash")
        auth_date=int(fields["auth_date"])
        now=time.time()
        if abs(now-auth_date)>3600 or auth_date>now+60:
            raise ValueError("expired")
        data_check="\n".join(f"{key}={value}" for key,value in sorted(fields.items()))
        secret=hmac.new(b"WebAppData",os.environ["BOT_TOKEN"].encode(),hashlib.sha256).digest()
        expected=hmac.new(secret,data_check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sent_hash.lower()):
            raise ValueError("bad signature")
        user=json.loads(fields["user"])
        telegram_id=int(user["id"])
        if telegram_id<=0:
            raise ValueError("invalid id")
    except (KeyError,ValueError,TypeError,OverflowError,UnicodeDecodeError):
        raise HTTPException(401,"Проверка Telegram не пройдена")
    async with SessionLocal() as session:
        staff=(await session.execute(text("""
            SELECT a.id,a.username,a.role,l.telegram_id
            FROM support_telegram_links l
            JOIN admins a ON a.id=l.admin_id
            WHERE l.telegram_id=:telegram_id
              AND a.enabled=TRUE AND a.role='analyst'
        """),{"telegram_id":telegram_id})).mappings().first()
    if not staff:
        raise HTTPException(403,"Нет доступа к панели аналитика")
    return dict(staff)

@router.get("")
async def analyst_page():
    file=Path(__file__).resolve().parent/"templates"/"analyst_miniapp.html"
    return FileResponse(file,media_type="text/html",headers={
        "Cache-Control":"no-store",
        "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        "Referrer-Policy":"no-referrer","X-Content-Type-Options":"nosniff",
    })

@router.get("/api/me")
async def analyst_me(staff=Depends(require_analyst)):
    return {"username":staff["username"],"role":staff["role"]}

@router.get("/api/report")
async def analyst_report(period:int=30,staff=__import__("fastapi").Depends(require_analyst)):
    if period not in (7,30,90,180):
        raise HTTPException(400,"Недопустимый период")
    data=await analytics_report(period)
    data["registrations_by_day"]=[
        {**row,"day":row["day"].isoformat() if hasattr(row["day"],"isoformat") else str(row["day"])}
        for row in data["registrations_by_day"]
    ]
    data["by_currency"]=[
        {**row,"period":row["period"].isoformat() if hasattr(row["period"],"isoformat") else str(row["period"])}
        for row in data["by_currency"]
    ]
    return data

def _build_analyst_csv(data,period):
    import csv,io
    from datetime import datetime
    output=io.StringIO()
    writer=csv.writer(output)
    writer.writerow(["Раздел","Показатель","Значение","Период"])
    labels={"total_users":"Всего пользователей","buyers":"Подтверждённые покупатели",
            "paid_events":"Подтверждённые оплаты за всё время","repeat_buyers":"Повторные покупатели",
            "reversals":"Возвраты и оспаривания за всё время",
            "registered_period":f"Регистрации за {period} дней",
            "payments_period":f"Подтверждённые оплаты за {period} дней"}
    for key,label in labels.items():
        writer.writerow(["Сводка",label,data["totals"].get(key,0),
                         f"{period} дней" if key.endswith("_period") else "Всё время"])
    writer.writerow([])
    writer.writerow(["Регистрации по дням","Дата","Количество",f"Период {period} дней"])
    for row in data["registrations_by_day"]:
        writer.writerow(["Регистрации",row["day"],row["count"],f"{period} дней"])
    writer.writerow([])
    writer.writerow(["Оплаты","Провайдер","Валюта","Количество","Сумма","Возвраты"])
    for row in data["by_currency"]:
        writer.writerow(["Оплаты",row["provider"],row["currency"] or "Не указана",
                         row["payments"],row["gross_amount"],row["reversals"]])
    filename=f"analyst-report-{period}d-{datetime.utcnow():%Y%m%d}.csv"
    return "\\ufeff"+output.getvalue(),filename


def _sign_analyst_export_payload(payload):
    raw=json.dumps(payload,separators=(",",":"),sort_keys=True).encode()
    encoded=base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature=hmac.new(os.environ["BOT_TOKEN"].encode(),encoded.encode(),hashlib.sha256).hexdigest()
    return encoded+"."+signature


def _read_analyst_export_token(token):
    try:
        encoded,signature=token.split(".",1)
        expected=hmac.new(os.environ["BOT_TOKEN"].encode(),encoded.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,signature):
            raise ValueError("signature")
        raw=base64.urlsafe_b64decode(encoded+"="*((4-len(encoded)%4)%4))
        payload=json.loads(raw)
        if not isinstance(payload,dict) or int(payload["exp"])<int(time.time()):
            raise ValueError("expired")
        if int(payload["period"]) not in (7,30,90,180):
            raise ValueError("period")
        return payload
    except Exception:
        raise HTTPException(403,"Ссылка на скачивание недействительна или устарела")


@router.get("/api/export")
async def analyst_export(period:int=30,staff=__import__("fastapi").Depends(require_analyst)):
    if period not in (7,30,90,180):
        raise HTTPException(400,"Недопустимый период")
    data=await analytics_report(period)
    csv_text,filename=_build_analyst_csv(data,period)
    return StreamingResponse(iter([csv_text]),media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":f'attachment; filename="{filename}"',
                 "Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})


@router.post("/api/export-link")
async def analyst_export_link(request:Request,period:int=30,staff=__import__("fastapi").Depends(require_analyst)):
    if period not in (7,30,90,180):
        raise HTTPException(400,"Недопустимый период")
    payload={"admin_id":int(staff["id"]),"telegram_id":int(staff["telegram_id"]),
             "period":period,"exp":int(time.time())+120,"nonce":secrets.token_urlsafe(12)}
    token=_sign_analyst_export_payload(payload)
    filename=f"analyst-report-{period}d.csv"
    return {"url":str(request.url_for("analyst_export_file",token=token)),"filename":filename}


@router.get("/api/export-file/{token}",name="analyst_export_file")
async def analyst_export_file(token:str):
    payload=_read_analyst_export_token(token)
    async with SessionLocal() as session:
        staff=(await session.execute(text("""
            SELECT id FROM admins
            WHERE id=:admin_id AND enabled=TRUE AND role='analyst'
        """),{"admin_id":int(payload["admin_id"])})).first()
        linked=(await session.execute(text("""
            SELECT 1 FROM support_telegram_links
            WHERE admin_id=:admin_id AND telegram_id=:telegram_id
        """),{"admin_id":int(payload["admin_id"]),"telegram_id":int(payload["telegram_id"])})).first()
    if not staff or not linked:
        raise HTTPException(403,"Доступ к панели аналитика отозван")
    period=int(payload["period"])
    data=await analytics_report(period)
    csv_text,filename=_build_analyst_csv(data,period)
    return StreamingResponse(iter([csv_text]),media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":f'attachment; filename="{filename}"',
                 "Cache-Control":"no-store","Pragma":"no-cache",
                 "X-Content-Type-Options":"nosniff",
                 "Access-Control-Allow-Origin":"https://web.telegram.org",
                 "Vary":"Origin"})
