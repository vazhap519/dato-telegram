"""Read-only analyst Telegram Mini App. No web-admin password/session is used."""
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import text

from db.database import SessionLocal
from db.admin_analytics import report as analytics_report

router=APIRouter(prefix="/analyst",tags=["analyst-miniapp"])

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
            SELECT a.id,a.username,a.role
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
async def analyst_me(staff=__import__("fastapi").Depends(require_analyst)):
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

@router.get("/api/export")
async def analyst_export(period:int=30,staff=__import__("fastapi").Depends(require_analyst)):
    if period not in (7,30,90,180):
        raise HTTPException(400,"Недопустимый период")
    import csv,io
    from datetime import datetime
    data=await analytics_report(period)
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
    return StreamingResponse(iter(["\ufeff"+output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":f'attachment; filename="{filename}"',
                 "Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
