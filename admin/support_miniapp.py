"""Authenticated Telegram Mini App for the existing support system.

No admin cookies or public username authentication: Telegram initData is
HMAC-verified on every API call, then bound staff role is checked in DB.
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from aiogram import Bot
from sqlalchemy import text

from db.database import SessionLocal
from db.support import (list_tickets, ticket_detail, admin_reply,
                        mark_delivery, set_ticket_status, support_media_record,
                        STATUSES)
from db.support_telegram import telegram_staff

log=logging.getLogger(__name__)
router=APIRouter(prefix="/tg-support",tags=["telegram-support"])


async def require_staff(x_telegram_init_data: str = Header(default="")):
    if not x_telegram_init_data or len(x_telegram_init_data)>8192:
        raise HTTPException(401,"Откройте панель из Telegram")
    try:
        fields=dict(parse_qsl(x_telegram_init_data,keep_blank_values=True,strict_parsing=True))
        sent_hash=fields.pop("hash")
        if len(sent_hash)!=64 or not all(ch in "0123456789abcdefABCDEF" for ch in sent_hash):
            raise ValueError("hash")
        auth_date=int(fields["auth_date"])
        if abs(time.time()-auth_date)>3600 or auth_date>time.time()+60:
            raise ValueError("expired")
        data_check="\n".join(f"{key}={value}" for key,value in sorted(fields.items()))
        secret=hmac.new(b"WebAppData",os.environ["BOT_TOKEN"].encode(),hashlib.sha256).digest()
        expected=hmac.new(secret,data_check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,sent_hash.lower()):
            raise ValueError("bad signature")
        user=json.loads(fields["user"])
        user_id=int(user["id"])
        if user_id<=0:
            raise ValueError("invalid id")
    except (KeyError,ValueError,TypeError,OverflowError,UnicodeDecodeError):
        raise HTTPException(401,"Проверка Telegram не пройдена")
    staff=await telegram_staff(user_id)
    if not staff:
        raise HTTPException(403,"Telegram не привязан к действующему сотруднику")
    return staff


class ReplyPayload(BaseModel):
    text: str = Field(min_length=1,max_length=3000)

class StatusPayload(BaseModel):
    status: str

@router.get("")
async def miniapp_page():
    file=Path(__file__).resolve().parent/"templates"/"support_miniapp.html"
    return FileResponse(file,media_type="text/html",headers={
        "Cache-Control":"no-store",
        "Content-Security-Policy":"default-src 'self'; script-src 'self' https://telegram.org 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org",
        "Referrer-Policy":"no-referrer",
        "X-Content-Type-Options":"nosniff",
    })

@router.get("/api/me")
async def current_staff(staff=Depends(require_staff)):
    return {"username":staff["username"],"role":staff["role"],
            "can_reply":staff["can_reply"],"can_change_status":staff["can_change_status"],
            "can_close":staff["can_close"],"can_delete":staff["can_delete"]}

@router.get("/api/tickets")
async def ticket_list(status:str="",staff=Depends(require_staff)):
    if status not in ("","open","pending","answered","closed"):
        raise HTTPException(400,"Неверный фильтр")
    tickets,total=await list_tickets(status=status,page=1)
    from db.support import support_counts
    counts=await support_counts()
    return {"tickets":[{
        "id":t["id"],"name":t["first_name"] or t["username"] or f"ID {t['telegram_id']}",
        "category":t["category"],"status":t["status"],
        "updated":t["updated_at"].isoformat(),"telegram_id":t["telegram_id"],
    } for t in tickets],"total":total,"counts":counts}

@router.get("/api/tickets/{ticket_id}")
async def ticket_messages(ticket_id:int,staff=Depends(require_staff)):
    if ticket_id<=0:raise HTTPException(400,"Неверный номер обращения")
    ticket,messages=await ticket_detail(ticket_id)
    if ticket is None:raise HTTPException(404,"Обращение не найдено")
    return {"ticket":{
        "id":ticket["id"],"name":ticket["first_name"] or ticket["username"] or str(ticket["telegram_id"]),
        "telegram_id":ticket["telegram_id"],"status":ticket["status"],"category":ticket["category"]},
        "messages":[{
            "id":m["id"],"actor":m["actor"],"text":m["body"],
            "media_type":m.get("media_type","text"),"has_media":bool(m.get("file_id")) and not m.get("deleted_at"),
            "delivery_status":m["delivery_status"],"deleted":bool(m.get("deleted_at")),
            "created":m["created_at"].isoformat()
        } for m in messages]}

@router.get("/api/media/{message_id}")
async def media(message_id:int,staff=Depends(require_staff)):
    record=await support_media_record(message_id)
    if not record:raise HTTPException(404,"Вложение не найдено")
    from io import BytesIO
    from fastapi.responses import StreamingResponse
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            info=await bot.get_file(record["file_id"])
            buffer=BytesIO()
            await bot.download_file(info.file_path,destination=buffer)
            buffer.seek(0)
        types={"photo":"image/jpeg","voice":"audio/ogg","document":"application/octet-stream"}
        return Response(content=buffer.getvalue(),
                        media_type=types.get(record["media_type"],"application/octet-stream"),
                        headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
    except Exception:
        log.exception("Miniapp support media failed")
        raise HTTPException(502,"Не удалось загрузить вложение")

@router.post("/api/tickets/{ticket_id}/reply")
async def reply(ticket_id:int,payload:ReplyPayload,staff=Depends(require_staff)):
    if not staff["can_reply"]:
        raise HTTPException(403,"Нет разрешения отвечать на обращения")
    try:
        uid,mid=await admin_reply(ticket_id,staff["username"],payload.text)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    sent=False
    delivered_id=None
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            delivered=await bot.send_message(uid,payload.text.strip(),parse_mode=None)
            sent=True
            delivered_id=delivered.message_id
    except Exception:
        log.exception("Miniapp support delivery failed ticket=%s",ticket_id)
    await mark_delivery(mid,sent,delivered_id)
    return {"sent":sent,"message_id":mid}

@router.post("/api/tickets/{ticket_id}/status")
async def change_status(ticket_id:int,payload:StatusPayload,staff=Depends(require_staff)):
    if payload.status not in ("open","pending","closed"):
        raise HTTPException(400,"Неверный статус")
    if payload.status=="closed" and not staff["can_close"]:
        raise HTTPException(403,"Нет разрешения закрывать обращения")
    if payload.status!="closed" and not staff["can_change_status"]:
        raise HTTPException(403,"Нет разрешения изменять статус")
    try:
        uid=await set_ticket_status(ticket_id,payload.status)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    if payload.status=="closed":
        try:
            async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
                await bot.send_message(uid,"✅ Ваш вопрос закрыт. Если нужна помощь, снова откройте чат поддержки.")
        except Exception:
            log.warning("Miniapp closure notice failed ticket=%s",ticket_id)
    return {"status":payload.status}
