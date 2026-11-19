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

from fastapi import APIRouter, Header, HTTPException, Depends
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
    return "\ufeff"+output.getvalue(),filename



def _build_analyst_xlsx(data,period):
    from datetime import date,datetime
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Alignment,Border,Font,PatternFill,Side
    from openpyxl.utils import get_column_letter

    wb=Workbook()
    wb.properties.title=f"Аналитический отчёт за {period} дней"
    wb.properties.subject="Сводка, регистрации, оплаты и удержание"
    wb.properties.creator="Круг Души"
    navy="17263B"; blue="30466A"; gold="D7B56D"
    pale="EEF2F7"; white="FFFFFF"; muted="64748B"; line="D9E1EA"
    thin=Side(style="thin",color=line)

    def make_sheet(name,title,subtitle,headers,rows,widths):
        ws=wb.active if name=="Сводка" else wb.create_sheet()
        ws.title=name
        last=get_column_letter(len(headers))
        ws.merge_cells(f"A1:{last}1")
        ws["A1"]=title
        ws["A1"].font=Font(name="Aptos",size=16,bold=True,color=white)
        ws["A1"].fill=PatternFill("solid",fgColor=navy)
        ws["A1"].alignment=Alignment(vertical="center")
        ws.row_dimensions[1].height=32
        ws.merge_cells(f"A2:{last}2")
        ws["A2"]=subtitle
        ws["A2"].font=Font(name="Aptos",size=10,color=muted,italic=True)
        ws["A2"].alignment=Alignment(vertical="center")
        ws.row_dimensions[2].height=23
        for col,heading in enumerate(headers,1):
            cell=ws.cell(4,col,heading)
            cell.font=Font(name="Aptos",bold=True,color=white)
            cell.fill=PatternFill("solid",fgColor=blue)
            cell.alignment=Alignment(vertical="center",wrap_text=True)
            cell.border=Border(bottom=Side(style="medium",color=gold))
        ws.row_dimensions[4].height=25
        for ridx,row in enumerate(rows,5):
            for cidx,value in enumerate(row,1):
                cell=ws.cell(ridx,cidx,value)
                cell.font=Font(name="Aptos",size=10,color="243247")
                cell.alignment=Alignment(vertical="center")
                cell.border=Border(bottom=thin)
                if ridx%2==1:
                    cell.fill=PatternFill("solid",fgColor=pale)
            ws.row_dimensions[ridx].height=21
        ws.freeze_panes="A5"
        if rows:
            ws.auto_filter.ref=f"A4:{last}{4+len(rows)}"
        for idx,width in enumerate(widths,1):
            ws.column_dimensions[get_column_letter(idx)].width=width
        ws.sheet_view.showGridLines=False
        ws.sheet_properties.pageSetUpPr.fitToPage=True
        ws.page_setup.fitToWidth=1
        ws.page_setup.fitToHeight=0
        return ws

    totals=data["totals"]
    summary_rows=[
        ("Всего пользователей",int(totals.get("total_users") or 0),"За всё время"),
        ("Новые регистрации",int(totals.get("registered_period") or 0),f"За {period} дней"),
        ("Покупатели",int(totals.get("buyers") or 0),"Уникальные пользователи с подтверждённой оплатой"),
        ("Подтверждённые оплаты",int(totals.get("paid_events") or 0),"Количество операций за всё время"),
        ("Оплаты за период",int(totals.get("payments_period") or 0),f"За {period} дней"),
        ("Повторные покупатели",int(totals.get("repeat_buyers") or 0),"Две и более подтверждённые оплаты"),
        ("Возвраты и оспаривания",int(totals.get("reversals") or 0),"За всё время"),
    ]
    ws=make_sheet("Сводка","АНАЛИТИЧЕСКИЙ ОТЧЁТ",
        f"Период: последние {period} дней · Сформировано: {datetime.now():%d.%m.%Y %H:%M}",
        ["Показатель","Значение","Комментарий"],summary_rows,[34,18,62])
    for row in range(5,5+len(summary_rows)):
        ws.cell(row,2).number_format="#,##0"

    registrations=[]
    for item in data.get("registrations_by_day",[]):
        day=item["day"]
        if isinstance(day,str):
            day=date.fromisoformat(day[:10])
        registrations.append((day,int(item.get("count") or 0)))
    ws=make_sheet("Регистрации","РЕГИСТРАЦИИ ПО ДНЯМ",
        f"Количество новых пользователей · последние {period} дней",
        ["Дата","Количество регистраций"],registrations,[22,28])
    for row in range(5,5+len(registrations)):
        ws.cell(row,1).number_format="DD.MM.YYYY"
        ws.cell(row,2).number_format="#,##0"

    payments=[]
    for item in data.get("by_currency",[]):
        month=item.get("period")
        if isinstance(month,str):
            month=date.fromisoformat(month[:10])
        elif isinstance(month,datetime):
            month=month.date()
        payments.append((month,item.get("provider") or "Не указан",
            item.get("currency") or "Не указана",int(item.get("payments") or 0),
            float(item.get("gross_amount") or 0),int(item.get("reversals") or 0)))
    ws=make_sheet("Оплаты","ПОДТВЕРЖДЁННЫЕ ОПЛАТЫ",
        "Помесячная разбивка за последние 12 месяцев · суммы не конвертируются между валютами",
        ["Месяц","Платёжная система","Валюта","Количество оплат","Сумма","Возвраты"],
        payments,[18,24,16,20,20,16])
    for row in range(5,5+len(payments)):
        ws.cell(row,1).number_format="MM.YYYY"
        ws.cell(row,4).number_format="#,##0"
        ws.cell(row,5).number_format="#,##0.00"
        ws.cell(row,6).number_format="#,##0"

    funnel=data.get("funnel") or {}
    retention=data.get("retention") or {}
    details=[
        ("Воронка","Запустили бота",int(funnel.get("started") or 0),"За всё время"),
        ("Воронка","Приняли оферту",int(funnel.get("accepted") or 0),"Уникальные пользователи"),
        ("Воронка","Нажали оформление",int(funnel.get("clicked") or 0),"Уникальные пользователи"),
        ("Воронка","Покупатели",int(funnel.get("buyers") or 0),"Уникальные пользователи с оплатой"),
        ("Удержание","Повторная оплата ≥30 дней",int(retention.get("repeat_30d") or 0),
         f"Из {int(retention.get('eligible_30d') or 0)} покупателей, у которых прошло 30 дней"),
        ("Удержание","Повторная оплата ≥90 дней",int(retention.get("repeat_90d") or 0),
         f"Из {int(retention.get('eligible_90d') or 0)} покупателей, у которых прошло 90 дней"),
        ("Удержание","Повторная оплата ≥180 дней",int(retention.get("repeat_180d") or 0),
         f"Из {int(retention.get('eligible_180d') or 0)} покупателей, у которых прошло 180 дней"),
    ]
    ws=make_sheet("Воронка и удержание","ВОРОНКА И УДЕРЖАНИЕ",
        "Накопительные показатели · повторная оплата не означает непрерывное участие",
        ["Раздел","Показатель","Значение","Комментарий"],details,[18,36,18,66])
    for row in range(5,5+len(details)):
        ws.cell(row,3).number_format="#,##0"

    output=BytesIO()
    wb.save(output)
    output.seek(0)
    filename=f"analyst-report-{period}d-{datetime.now():%Y%m%d}.xlsx"
    return output.getvalue(),filename


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
        if payload.get("format","csv") not in ("csv","xlsx"):
            raise ValueError("format")
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
async def analyst_export_link(period:int=30,format:str="csv",staff=__import__("fastapi").Depends(require_analyst)):
    if period not in (7,30,90,180):
        raise HTTPException(400,"Недопустимый период")
    if format not in ("csv","xlsx"):
        raise HTTPException(400,"Недопустимый формат")
    payload={"admin_id":int(staff["id"]),"telegram_id":int(staff["telegram_id"]),
             "period":period,"format":format,"exp":int(time.time())+120,"nonce":secrets.token_urlsafe(12)}
    token=_sign_analyst_export_payload(payload)
    filename=f"analyst-report-{period}d.{format}"
    return {"url":"https://bot.arutyun.me/tg-support/analyst/api/export-file/"+token,"filename":filename}


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
    if payload.get("format","csv")=="xlsx":
        file_bytes,filename=_build_analyst_xlsx(data,period)
        return StreamingResponse(iter([file_bytes]),media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition":f'attachment; filename="{filename}"',
                     "Cache-Control":"no-store","Pragma":"no-cache",
                     "X-Content-Type-Options":"nosniff",
                     "Access-Control-Allow-Origin":"https://web.telegram.org","Vary":"Origin"})
    csv_text,filename=_build_analyst_csv(data,period)
    return StreamingResponse(iter([csv_text]),media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":f'attachment; filename="{filename}"',
                 "Cache-Control":"no-store","Pragma":"no-cache",
                 "X-Content-Type-Options":"nosniff",
                 "Access-Control-Allow-Origin":"https://web.telegram.org","Vary":"Origin"})
