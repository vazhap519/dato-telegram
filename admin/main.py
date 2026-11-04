
from fastapi import (
    FastAPI,
    Request,
    Form,
    Depends,
    Cookie,
    HTTPException,
    WebSocket
)

from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from db.database import SessionLocal
from db.admin_extensions import initialize_extensions, create_task, tasks as crm_tasks, update_task, task_notification_settings, update_task_notification_settings, global_search, failure_queue, operations, audit as admin_action_audit
from db.dashboard import dashboard_data, event_journal, EVENT_TYPES, initialize_health_snapshots, last_health_snapshot
from db.subscriber_crm import initialize_crm, crm_timeline, direct_message_history, begin_direct_message, finish_direct_message, crm_tags, direct_message_for_delete, mark_direct_message_deleted, clear_visible_crm_history
from db.subscriber_profile import initialize_subscriber_profiles, get_subscriber_profile, save_subscriber_profile, subscriber_verified_payments, subscriber_access_summary
from db.reliability_settings import initialize_reliability_settings, get_reliability_settings, save_reliability_settings
from db.tribute_webhooks import MAX_BODY_BYTES, InvalidTributeEvent, parse_signed_delivery, verify_signature, store_event, recent_events
from db.payment_providers import initialize_payment_providers, providers as list_payment_providers, save_provider, register_provider, payment_provider_counts, archive_provider, archived_providers, restore_provider
from db.support import initialize_support, list_tickets, ticket_detail, support_counts, admin_reply, mark_delivery, failed_support_message, support_media_record, set_ticket_status, support_message_for_delete, mark_support_message_deleted, CATEGORIES as SUPPORT_CATEGORIES, STATUSES as SUPPORT_STATUSES
from db.expiry_monitor import initialize_expiry_monitor, expiry_overview, expiry_counts
from db.subscription_automation import initialize_subscription_automation, automation_settings, save_automation_settings, managed_count, member_removal_diagnostics
from db.group_moderation import initialize_group_moderation, get_moderation_settings, save_moderation_settings, moderation_stats, save_content_settings, moderation_history, moderation_summary, clear_user_warnings, moderation_offenders, moderation_user_details, record_manual_action, moderation_manual_history, set_trusted_user
from db.group_access import initialize_group_access, group_settings, save_group, has_group_access, record_invite, latest_invites, clear_completed_invite_history, group_removal_test, set_group_removal_test
from db.tribute_payment_test import initialize_tribute_payment_tests, create_tribute_test_link, recent_tribute_test_links, TEST_SUBSCRIPTION_ID, TEST_PERIOD_ID
from db.tribute_test_activation import initialize_tribute_test_activation, activate_saved_test_purchase, TributeActivationError
from db.tribute_auto import initialize_tribute_auto, list_auto_settings, register_auto_offer, set_auto_offer_enabled, set_auto_processing_enabled, is_auto_processing_enabled, AutoPaymentError
from db.tribute_shared_checkout import initialize_shared_checkout, shared_checkout_status, set_shared_checkout_enabled, CheckoutNotReady
from db.tribute_paid_removal import initialize_tribute_paid_removal, tribute_paid_removal_status, set_tribute_paid_removal_enabled, TributeRemovalError
from db.subscriptions import initialize_subscriptions, overview as subscriptions_overview, people as subscription_people, person as subscription_person, grant_access, revoke_grant, update_plan, create_plan, archive_plan, change_grant_expiry, extend_community_access, clear_visible_audit, clear_revoked_grant_history
from db.broadcast import initialize_broadcasts, count_recipients, create_broadcast, list_broadcasts, AUDIENCES
from db.automation import initialize_automation, templates as list_message_templates, upsert_template, rules, set_rule, TRIGGERS
from db.queries import get_users_count, get_settings
from db.flow import initialize_flow, get_flow_settings, save_flow_settings, registration_stats, create_upload_code, list_onboarding_users
from .auth import (
    verify_password,
    create_token,
    decode_token,
    hash_password
)
from .websocket import connections
import hmac
import hashlib
import os

def make_csrf_token(session_token: str) -> str:
    secret = os.environ["SECRET_KEY"].encode()
    return hmac.new(
        secret,
        session_token.encode(),
        hashlib.sha256
    ).hexdigest()


app = FastAPI()

@app.on_event('startup')
async def setup_onboarding():
    await initialize_flow()
    await initialize_support()
    await initialize_broadcasts()
    await initialize_automation()
    await initialize_subscriptions()
    await initialize_health_snapshots()
    await initialize_subscriber_profiles()
    await initialize_crm()
    await initialize_extensions()
    await initialize_tribute_payment_tests()
    await initialize_tribute_test_activation()
    await initialize_tribute_auto()
    await initialize_shared_checkout()
    await initialize_payment_providers()
    await initialize_reliability_settings()
    await initialize_group_access()
    await initialize_group_moderation()
    await initialize_expiry_monitor()
    await initialize_subscription_automation()
    await initialize_tribute_paid_removal()


# Protection against cross-origin admin requests
@app.middleware("http")
async def protect_admin_forms(request: Request, call_next):
    protected_paths = {
        "/update",
        "/update-flow",
        "/create-video-upload",
        "/broadcast/preview",
        "/broadcast/send",
        "/broadcast/template",
        "/broadcast/rule",
        "/reliability/settings",
        "/reliability/test",
        "/moderation/settings",
        "/moderation/content",
        "/moderation/reset",
        "/moderation/manual",
        "/moderation/trusted",
        "/subscriptions/grant",
        "/subscriptions/profile",
        "/crm/task/create",
        "/crm/task/status",
        "/crm/task/alerts",
        "/subscriptions/message",
        "/subscriptions/message/delete",
        "/subscriptions/history/clear",
        "/subscriptions/revoke",
        "/subscriptions/audit/clear",
        "/subscriptions/invites/history/clear",
        "/subscriptions/grants/history/clear",
        "/subscriptions/grant/expiry",
        "/subscriptions/renew",
        "/subscriptions/automation",
        "/subscriptions/tribute-test/create",
        "/subscriptions/tribute-test/activate",
        "/subscriptions/tribute-auto/setting",
        "/subscriptions/tribute-auto/offer",
        "/subscriptions/tribute-auto/add",
        "/subscriptions/tribute-auto/checkout",
        "/subscriptions/tribute-auto/removal",
        "/subscriptions/provider",
        "/subscriptions/provider/add",
        "/subscriptions/provider/delete",
        "/subscriptions/provider/restore",
        "/subscriptions/removal-test",
        "/subscriptions/plan",
        "/subscriptions/plan/create",
        "/subscriptions/plan/delete",
        "/subscriptions/group",
        "/subscriptions/invite",
        "/change-credentials",
        "/support/reply",
    }

    if request.method == "POST" and request.url.path.startswith("/support/"):
        protected_paths.add(request.url.path)
    if (
        request.method == "POST"
        and request.url.path in protected_paths
    ):
        origin = request.headers.get("origin")

        if origin != "https://bot.arutyun.me":
            raise_response = HTTPException(
                status_code=403,
                detail="Forbidden origin"
            )
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=raise_response.status_code,
                content={"detail": raise_response.detail}
            )

    return await call_next(request)


@app.post("/webhooks/tribute")
async def tribute_webhook(request: Request):
    """Stage 1: verify and persist, NEVER grant/revoke access."""
    api_key = os.environ.get("TRIBUTE_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="Webhook not configured")
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        raise HTTPException(status_code=415, detail="Expected application/json")
    # Stream with a hard cap so even missing Content-Length cannot exhaust memory.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
    body = bytes(raw)
    if not verify_signature(body, request.headers.get("trbt-signature"), api_key):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    try:
        event = parse_signed_delivery(body)
    except InvalidTributeEvent as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    inserted = await store_event(event)
    return {"status": "received" if inserted else "duplicate", "access_changes": False}


templates = Jinja2Templates(directory="admin/templates")

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SITE_TZ = ZoneInfo("Asia/Tbilisi")

def tbilisi_time(value, pattern="%d.%m.%Y %H:%M"):
    """Convert persisted UTC-aware timestamps to the site's local time."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(SITE_TZ).strftime(pattern)

templates.env.filters["tbilisi_time"] = tbilisi_time


# 🔐 AUTH
def get_current_user(access_token: str = Cookie(None)):
    if not access_token:
        raise HTTPException(status_code=401)

    return decode_token(access_token)


# 🌐 ROOT
@app.get("/")
async def root():
    return RedirectResponse(url="/login")


# 🔐 LOGIN PAGE
@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={}
    )


# 🔐 LOGIN LOGIC
@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...)
):
    try:
        async with SessionLocal() as session:

            # ✅ CREATE TABLE
            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS admins (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE,
                    password TEXT
                )
            """))

            await session.commit()

            # ✅ LOGIN CHECK
            result = await session.execute(
                text("SELECT * FROM admins WHERE username=:u"),
                {"u": username}
            )

            user = result.fetchone()

            if user:
                user = user._mapping

            if not user:
                return templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={
                        "error": "Wrong credentials"
                    }
                )

            if not verify_password(password, user["password"]):
                return templates.TemplateResponse(
                    request=request,
                    name="login.html",
                    context={
                        "error": "Wrong credentials"
                    }
                )

            # ✅ CREATE TOKEN
            token = create_token({
                "user": username
            })

            response = RedirectResponse(
                url="/dashboard",
                status_code=302
            )

            response.set_cookie(
                key="access_token",
                value=token,
                httponly=True,
                secure=True,
                samesite="strict",
                max_age=43200,
                path="/"
            )

            return response

    except Exception as e:
        return {
            "error": str(e)
        }


# CSRF token validation
async def verify_csrf(
    request: Request,
    csrf_token: str = Form(""),
    access_token: str = Cookie(None)
):
    if not access_token or not csrf_token:
        raise HTTPException(
            status_code=403,
            detail="Missing CSRF token"
        )

    expected = make_csrf_token(access_token)

    if not hmac.compare_digest(csrf_token, expected):
        raise HTTPException(
            status_code=403,
            detail="Invalid CSRF token"
        )


# 📊 DASHBOARD
@app.get("/dashboard")
async def dashboard(request: Request,user=Depends(get_current_user)):
    stats=await registration_stats()
    overview,recent_events=await dashboard_data()
    health_snapshot=await last_health_snapshot()
    return templates.TemplateResponse(
        request=request,name="index.html",
        context={"stats":stats,"overview":overview,"recent_events":recent_events,
                 "health":health_snapshot},
        headers={"Cache-Control":"no-store"}
    )


@app.get("/events")
async def event_journal_page(
    request: Request,search: str="",kind: str="all",
    date_from: str="",date_to: str="",page: int=1,
    user=Depends(get_current_user)
):
    from datetime import date
    from urllib.parse import urlencode
    def read_date(value):
        if not value: return None
        try: return date.fromisoformat(value)
        except ValueError: raise HTTPException(400,detail="Неверная дата")
    if kind not in EVENT_TYPES:
        kind="all"
    page=max(1,min(int(page),100000))
    try:
        items,total=await event_journal(search,kind,read_date(date_from),
                                        read_date(date_to),page)
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    options={"search":search,"kind":kind,"date_from":date_from,"date_to":date_to}
    return templates.TemplateResponse(
        request=request,name="events.html",context={
            "events":items,"total":total,"page":page,
            "pages":max(1,(total+29)//30),"search":search,"kind":kind,
            "date_from":date_from,"date_to":date_to,"event_types":EVENT_TYPES,
            "prev_url":"/events?"+urlencode({**options,"page":page-1}),
            "next_url":"/events?"+urlencode({**options,"page":page+1})
        },headers={"Cache-Control":"no-store"}
    )


@app.get("/crm/tasks")
async def crm_tasks_page(request:Request,status:str="open",user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request=request,name="crm_tasks.html",context={
          "tasks":await crm_tasks(status=status),
          "status":status,"settings":await task_notification_settings(),
          "csrf_token":make_csrf_token(request.cookies.get("access_token","")),
        },headers={"Cache-Control":"no-store"}
    )

@app.post("/crm/task/create")
async def crm_task_create(
    telegram_id:int=Form(...),title:str=Form(...),details:str=Form(""),
    due_at:str=Form(...),assignee:str=Form(""),remind:bool=Form(False),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    from datetime import datetime,timezone
    from zoneinfo import ZoneInfo
    if telegram_id<=0:raise HTTPException(400,detail="Некорректный Telegram ID")
    try:
        due=datetime.fromisoformat(due_at)
        if due.tzinfo is not None:raise ValueError()
        due=due.replace(tzinfo=ZoneInfo("Asia/Tbilisi")).astimezone(timezone.utc)
        if due<datetime.now(timezone.utc):raise ValueError()
        await create_task(telegram_id,title,details,due,assignee or user["user"],remind,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail="Некорректная дата или поля задачи: "+str(exc))
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?task=created#subscriber-tasks",303)

@app.post("/crm/task/status")
async def crm_task_change(
    task_id:int=Form(...),telegram_id:int=Form(0),status:str=Form(...),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    try:
        await update_task(task_id,telegram_id or None,status,user["user"])
    except ValueError as exc:raise HTTPException(400,detail=str(exc))
    dest=f"/subscriptions/users/{telegram_id}#subscriber-tasks" if telegram_id else "/crm/tasks"
    return RedirectResponse(dest,303)

@app.post("/crm/task/alerts")
async def crm_alert_settings(
    enabled:bool=Form(False),telegram_id:str=Form(""),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    try:
        await update_task_notification_settings(enabled,int(telegram_id) if telegram_id.strip() else None,user["user"])
    except ValueError as exc:raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/crm/tasks?saved=1",303)

@app.get("/search")
async def admin_search_page(request:Request,q:str="",user=Depends(get_current_user)):
    return templates.TemplateResponse(request=request,name="admin_search.html",
        context={"q":q[:80],"results":await global_search(q)},
        headers={"Cache-Control":"no-store"})

@app.get("/operations")
async def operation_log_page(request:Request,user=Depends(get_current_user)):
    return templates.TemplateResponse(request=request,name="admin_operations.html",
        context={"operations":await operations()},
        headers={"Cache-Control":"no-store"})

@app.get("/issues")
async def issues_page(request:Request,user=Depends(get_current_user)):
    return templates.TemplateResponse(request=request,name="admin_issues.html",
        context={"failures":await failure_queue()},
        headers={"Cache-Control":"no-store"})

@app.get("/settings/bot")
async def bot_settings_page(request: Request,user=Depends(get_current_user)):
    flow=await get_flow_settings()
    return templates.TemplateResponse(
        request=request,name="bot_settings.html",
        context={
            "flow":flow,"csrf_token":make_csrf_token(
                request.cookies.get("access_token","")
            ),
        },headers={"Cache-Control":"no-store"}
    )


@app.get("/moderation")
async def moderation_page(request: Request, user=Depends(get_current_user)):
    cfg=await group_settings()
    return templates.TemplateResponse(
        request=request, name="moderation.html", context={
            "moderation": await get_moderation_settings(),
            "moderation_stats": await moderation_stats(),
            "moderation_history": await moderation_history(),
            "moderation_summary": await moderation_summary(),
            "manual_history": await moderation_manual_history(cfg["chat_id"]) if cfg.get("chat_id") else [],
            "tg_group": cfg,
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
    )


@app.post("/moderation/settings")
async def moderation_save(
    join_enabled: bool = Form(False),
    leave_enabled: bool = Form(False),
    pin_enabled: bool = Form(False),
    other_enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    await save_moderation_settings(
        join_enabled, leave_enabled, pin_enabled, other_enabled, user["user"]
    )
    return RedirectResponse("/moderation?saved=1",status_code=303)


@app.post("/moderation/content")
async def moderation_content_save(
    content_enabled: bool = Form(False),
    block_links: bool = Form(False),
    block_invites: bool = Form(False),
    block_forwards: bool = Form(False),
    block_flood: bool = Form(False),
    block_duplicates: bool = Form(False),
    block_media: bool = Form(False),
    allowed_domains: str = Form(""),
    blocked_words: str = Form(""),
    trusted_users: str = Form(""),
    observe_only: bool = Form(False),
    notify_user: bool = Form(False),
    flood_limit: int = Form(6),
    flood_window_seconds: int = Form(10),
    duplicate_window_seconds: int = Form(90),
    warning_limit: int = Form(3),
    mute_minutes: int = Form(0),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    try:
        await save_content_settings({
            "content_enabled":content_enabled,"block_links":block_links,
            "block_invites":block_invites,"block_forwards":block_forwards,
            "block_flood":block_flood,"block_duplicates":block_duplicates,
            "block_media":block_media,"allowed_domains":allowed_domains,
            "blocked_words":blocked_words,"trusted_users":trusted_users,
            "observe_only":observe_only,"notify_user":notify_user,"flood_limit":flood_limit,
            "flood_window_seconds":flood_window_seconds,
            "duplicate_window_seconds":duplicate_window_seconds,
            "warning_limit":warning_limit,"mute_minutes":mute_minutes,
        },user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/moderation?content_saved=1#anti-spam",status_code=303)


@app.post("/moderation/reset")
async def moderation_reset_user(
    telegram_id: int = Form(...), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "RESET_WARNINGS" or telegram_id<=0:
        raise HTTPException(400,detail="Подтвердите сброс предупреждений")
    config=await group_settings()
    if not config.get("chat_id"):
        raise HTTPException(400,detail="Telegram-группа не подключена")
    await clear_user_warnings(config["chat_id"],telegram_id)
    await record_manual_action(config["chat_id"],telegram_id,user["user"],
                               "reset","success")
    return RedirectResponse("/moderation?reset=1#moderation-history",status_code=303)


@app.get("/moderation/lookup")
async def moderation_lookup(telegram_id: int, user=Depends(get_current_user)):
    if telegram_id <= 0 or telegram_id > 9999999999999:
        raise HTTPException(400,detail="Укажите правильный Telegram ID")
    return RedirectResponse(f"/moderation/user/{telegram_id}",status_code=303)


@app.get("/moderation/offenders")
async def moderation_offenders_page(
    request: Request, search: str = "", page: int = 1,
    user=Depends(get_current_user)
):
    from urllib.parse import urlencode
    cfg=await group_settings()
    chat_id=cfg.get("chat_id")
    people,total=(await moderation_offenders(chat_id,search,page)) if chat_id else ([],0)
    page=max(1,min(int(page),10000))
    return templates.TemplateResponse(
        request=request,name="moderation_offenders.html",context={
            "tg_group":cfg,"people":people,"total":total,"search":search,
            "page":page,"pages":max(1,(total+24)//25),
            "prev_url":"/moderation/offenders?"+urlencode({"search":search,"page":page-1}),
            "next_url":"/moderation/offenders?"+urlencode({"search":search,"page":page+1}),
        },headers={"Cache-Control":"no-store"}
    )


@app.get("/moderation/user/{telegram_id}")
async def moderation_user_page(
    telegram_id: int, request: Request,
    user=Depends(get_current_user)
):
    if telegram_id<=0:
        raise HTTPException(400,detail="Некорректный Telegram ID")
    cfg=await group_settings()
    chat_id=cfg.get("chat_id")
    if not chat_id:
        raise HTTPException(400,detail="Сначала подключите группу")
    profile=await moderation_user_details(chat_id,telegram_id)
    settings=await get_moderation_settings()
    trusted={x.strip() for x in (settings.get("trusted_users") or "").splitlines()}
    member_status="Не удалось проверить"
    member_permissions=""
    from aiogram import Bot
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            member=await telegram.get_chat_member(chat_id,telegram_id)
            member_status=str(member.status)
            if member.status=="restricted" or getattr(member.status,"value","")=="restricted":
                member_permissions="Ограничен до: "+str(getattr(member,"until_date","—"))
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Unable to check moderation user status uid=%s",telegram_id)
    return templates.TemplateResponse(
        request=request,name="moderation_user.html",context={
            "uid":telegram_id,"profile":profile,"tg_group":cfg,
            "member_status":member_status,"member_permissions":member_permissions,
            "trusted":str(telegram_id) in trusted,
            "csrf_token":make_csrf_token(request.cookies.get("access_token","")),
        },headers={"Cache-Control":"no-store"}
    )


@app.post("/moderation/trusted")
async def moderation_trusted_update(
    telegram_id: int=Form(...),enabled: bool=Form(False),
    confirm: str=Form(""),csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm!="CHANGE_TRUST":
        raise HTTPException(400,detail="Подтвердите изменение исключения")
    try:
        await set_trusted_user(telegram_id,enabled,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    cfg=await group_settings()
    if cfg.get("chat_id"):
        await record_manual_action(cfg["chat_id"],telegram_id,user["user"],
                                   "trust_on" if enabled else "trust_off","success")
    return RedirectResponse(f"/moderation/user/{telegram_id}?trust=saved",status_code=303)


@app.post("/moderation/manual")
async def moderation_manual_action(
    telegram_id: int=Form(...),action: str=Form(...),
    duration_minutes: int=Form(15),reason: str=Form(""),
    confirm: str=Form(""),csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    from aiogram import Bot
    from aiogram.enums import ChatMemberStatus
    from aiogram.types import ChatPermissions
    from datetime import datetime,timedelta,timezone
    import logging
    if action not in ("mute","unmute","reset"):
        raise HTTPException(400,detail="Недопустимое действие")
    if confirm!="MODERATE":
        raise HTTPException(400,detail="Подтвердите действие")
    if telegram_id<=0 or telegram_id>9999999999999:
        raise HTTPException(400,detail="Некорректный Telegram ID")
    if len(reason)>500:
        raise HTTPException(400,detail="Слишком длинная причина")
    if action=="mute" and duration_minutes not in (5,15,30,60,180,1440,10080):
        raise HTTPException(400,detail="Выберите разрешённый срок ограничения")
    cfg=await group_settings()
    chat_id=cfg.get("chat_id")
    if not chat_id:
        raise HTTPException(400,detail="Группа не подключена")
    if action=="reset":
        await clear_user_warnings(chat_id,telegram_id)
        await record_manual_action(chat_id,telegram_id,user["user"],"reset",
                                   "success",details=reason)
        return RedirectResponse(f"/moderation/user/{telegram_id}?result=reset",status_code=303)
    status="failed"
    error=None
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            me=await telegram.get_me()
            admin=await telegram.get_chat_member(chat_id,me.id)
            can_restrict=(admin.status==ChatMemberStatus.CREATOR or (
                admin.status==ChatMemberStatus.ADMINISTRATOR
                and bool(getattr(admin,"can_restrict_members",False))
            ))
            if not can_restrict:
                raise ValueError("Боту необходимо право ограничивать участников")
            if telegram_id==me.id:
                raise ValueError("Нельзя ограничивать самого бота")
            target=await telegram.get_chat_member(chat_id,telegram_id)
            if target.status in (ChatMemberStatus.CREATOR,ChatMemberStatus.ADMINISTRATOR):
                raise ValueError("Нельзя изменять права администратора группы")
            if action=="mute":
                if target.status in (ChatMemberStatus.LEFT,ChatMemberStatus.KICKED):
                    raise ValueError("Участник не состоит в группе")
                await telegram.restrict_chat_member(
                    chat_id,telegram_id,
                    permissions=ChatPermissions(can_send_messages=False),
                    until_date=datetime.now(timezone.utc)+timedelta(minutes=duration_minutes),
                    use_independent_chat_permissions=False,
                )
            else:
                if target.status in (ChatMemberStatus.LEFT,ChatMemberStatus.KICKED):
                    raise ValueError("Участник не состоит в группе")
                # Restores group-default permissions for ordinary communication.
                chat=await telegram.get_chat(chat_id)
                permissions=getattr(chat,"permissions",None) or ChatPermissions(
                    can_send_messages=True,can_send_audios=True,
                    can_send_documents=True,can_send_photos=True,
                    can_send_videos=True,can_send_video_notes=True,
                    can_send_voice_notes=True,can_send_polls=True,
                    can_send_other_messages=True,can_add_web_page_previews=True,
                    can_invite_users=True
                )
                await telegram.restrict_chat_member(
                    chat_id,telegram_id,permissions=permissions,
                    use_independent_chat_permissions=False
                )
            status="success"
    except Exception as exc:
        error=str(exc)[:300]
        logging.getLogger(__name__).warning(
            "Manual moderation failed action=%s uid=%s: %s",action,telegram_id,error
        )
    await record_manual_action(chat_id,telegram_id,user["user"],action,status,
                               duration_minutes if action=="mute" else None,
                               reason,error)
    from urllib.parse import urlencode
    return RedirectResponse(
        f"/moderation/user/{telegram_id}?"+urlencode({"result":status,"action":action}),
        status_code=303
    )


@app.get("/reliability")
async def reliability_page(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request=request, name="reliability.html", context={
            "settings": await get_reliability_settings(),
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
    )


@app.post("/reliability/settings")
async def reliability_save(
    telegram_chat_id: str = Form(""), enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    try:
        chat_id = int(telegram_chat_id.strip()) if telegram_chat_id.strip() else None
        await save_reliability_settings(chat_id, enabled, user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/reliability?saved=1",status_code=303)


@app.post("/reliability/test")
async def reliability_test_alert(
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    config = await get_reliability_settings()
    if not config["enabled"] or not config["telegram_chat_id"]:
        return RedirectResponse("/reliability?test=disabled",status_code=303)
    from aiogram import Bot
    import logging
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            await telegram.send_message(
                config["telegram_chat_id"],
                "✅ Круг Души: тестовое уведомление мониторинга доставлено. "
                "Теперь сюда будут приходить предупреждения о сбоях сервера."
            )
    except Exception:
        logging.getLogger(__name__).exception("Reliability test alert failed")
        return RedirectResponse("/reliability?test=failed",status_code=303)
    return RedirectResponse("/reliability?test=sent",status_code=303)


@app.post("/create-video-upload")
async def create_video_upload(request: Request, csrf_check=Depends(verify_csrf), user=Depends(get_current_user)):
    import secrets
    code = secrets.token_urlsafe(24)
    await create_upload_code(code)
    return templates.TemplateResponse(
        request=request, name="bot_settings.html",
        context={
            "flow":await get_flow_settings(),
            "csrf_token":make_csrf_token(request.cookies.get("access_token","")),
            "upload_command":"/upload "+code,
        },headers={"Cache-Control":"no-store"}
    )


@app.get("/users")
async def users_page(
    request: Request,
    stage: str = "started",
    search: str = "",
    page: int = 1,
    user=Depends(get_current_user)
):
    from urllib.parse import urlencode
    if stage not in ("started", "accepted", "clicked"):
        stage = "started"
    page = max(1, min(page, 100000))
    users, total = await list_onboarding_users(stage, search, page)
    stats = await registration_stats()
    def page_url(p):
        return "/users?" + urlencode({"stage": stage, "search": search, "page": p})
    return templates.TemplateResponse(
        request=request, name="users.html",
        context={
            "user": user, "users": users, "total": total,
            "stats": stats, "stage": stage, "search": search,
            "page": page, "page_count": max(1, (total + 29) // 30),
            "prev_url": page_url(page - 1),
            "next_url": page_url(page + 1)
        },
        headers={"Cache-Control": "no-store"}
    )



# Independent subscription registry. Phase 1: administrative records only,
# intentionally no payment creation, invitation, or group member removal.
@app.get("/support")
async def support_inbox(request: Request, status: str="", search: str="", page: int=1,
                        user=Depends(get_current_user)):
    page=max(1,page)
    rows,total=await list_tickets(status,search,page)
    counts=await support_counts()
    return templates.TemplateResponse(request=request,name="support.html",context={
        "tickets":rows,"total":total,"page":page,"status":status,"search":search,
        "counts":counts,"statuses":SUPPORT_STATUSES,"categories":SUPPORT_CATEGORIES,
    })


@app.get("/support/media/{message_id}")
async def support_media_download(message_id:int,user=Depends(get_current_user)):
    """Serve only authenticated admins; never expose the Telegram bot token."""
    from fastapi.responses import StreamingResponse
    from io import BytesIO
    from aiogram import Bot
    media=await support_media_record(message_id)
    if not media: raise HTTPException(404,detail="Вложение не найдено")
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            info=await bot.get_file(media["file_id"])
            stream=BytesIO()
            await bot.download_file(info.file_path,destination=stream)
            stream.seek(0)
        content_types={"photo":"image/jpeg","voice":"audio/ogg","document":"application/octet-stream"}
        return StreamingResponse(iter([stream.getvalue()]),
            media_type=content_types.get(media["media_type"],"application/octet-stream"),
            headers={"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"})
    except Exception:
        import logging
        logging.exception("Support attachment load failed %s",message_id)
        raise HTTPException(502,detail="Вложение не удалось загрузить")


@app.get("/support/{ticket_id}/messages")
async def support_new_messages(ticket_id:int,after:int=0,user=Depends(get_current_user)):
    from fastapi.responses import JSONResponse
    ticket,messages=await ticket_detail(ticket_id)
    if not ticket: raise HTTPException(404,detail="Диалог не найден")
    return JSONResponse({"status":ticket["status"],
        "deleted_ids":[m["id"] for m in messages if m.get("deleted_at")],
        "messages":[{
        "id":m["id"],"actor":m["actor"],"body":m["body"],
        "media_type":m.get("media_type","text"),
        "has_file":bool(m.get("file_id")),
        "delivery_status":m["delivery_status"],
        "deleted_at":bool(m.get("deleted_at")),
        "can_delete":not bool(m.get("deleted_at")) and m["delivery_status"]=="sent"
            and telegram_deletion_available(m.get("telegram_message_id"),
                m.get("telegram_sent_at") if m["actor"]!="user" else m["created_at"]),
        "time":tbilisi_time(m["created_at"])
    } for m in messages if m["id"]>max(0,after)]})


@app.get("/support/{ticket_id}")
async def support_thread(request: Request,ticket_id:int,
                         user=Depends(get_current_user)):
    ticket,messages=await ticket_detail(ticket_id)
    if not ticket: raise HTTPException(404,detail="Обращение не найдено")
    chats,_=await list_tickets(page=1)
    support_deletable_ids={m["id"] for m in messages if not m.get("deleted_at")
        and m["delivery_status"]=="sent"
        and telegram_deletion_available(m.get("telegram_message_id"),
            m.get("telegram_sent_at") if m["actor"]!="user" else m["created_at"])}
    return templates.TemplateResponse(request=request,name="support_ticket.html",context={
        "ticket":ticket,"messages":messages,"chats":chats,
        "support_deletable_ids":support_deletable_ids,
        "categories":SUPPORT_CATEGORIES,"statuses":SUPPORT_STATUSES,
        "csrf_token":make_csrf_token(request.cookies.get("access_token","")),
    })


@app.post("/support/{ticket_id}/delete/{message_id}")
async def support_delete_message(
    ticket_id:int,message_id:int,confirm:str=Form(""),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    from aiogram import Bot
    import logging
    if confirm!="DELETE_BOTH":
        raise HTTPException(400,detail="Подтвердите удаление у пользователя")
    row=await support_message_for_delete(ticket_id,message_id)
    if not row: raise HTTPException(404,detail="Сообщение не найдено")
    if row["deleted_at"]:
        return RedirectResponse(f"/support/{ticket_id}?delete=done",status_code=303)
    if row["delivery_status"] not in ("sent",):
        raise HTTPException(400,detail="Сообщение не было доставлено в Telegram")
    try:
        ensure_telegram_message_deletable(row["telegram_message_id"],row["telegram_sent_at"] if row["actor"]!="user" else row["created_at"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            await bot.delete_message(row["telegram_id"],row["telegram_message_id"])
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Support message deletion failed ticket=%s message=%s: %s",
            ticket_id,message_id,type(exc).__name__
        )
        return RedirectResponse(f"/support/{ticket_id}?delete=failed",status_code=303)
    await mark_support_message_deleted(ticket_id,message_id,user["user"])
    return RedirectResponse(f"/support/{ticket_id}?delete=done",status_code=303)


@app.post("/support/{ticket_id}/reply")
async def support_admin_reply(ticket_id:int,body:str=Form(...),
                              csrf_check=Depends(verify_csrf),
                              user=Depends(get_current_user)):
    from aiogram import Bot
    from bot.support_messages import format_staff_reply, reply_keyboard
    try: uid,mid=await admin_reply(ticket_id,user["user"],body)
    except ValueError as exc: raise HTTPException(400,detail=str(exc))
    sent=False
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            delivered=await bot.send_message(uid,
                body.strip(),
                parse_mode=None)
            tg_mid=delivered.message_id
            sent=True
    except Exception:
        import logging
        logging.exception("Support reply delivery failed for ticket %s",ticket_id)
    await mark_delivery(mid,sent,tg_mid if sent else None)
    return RedirectResponse(f"/support/{ticket_id}?sent={'ok' if sent else 'fail'}",status_code=303)


@app.post("/support/{ticket_id}/retry/{message_id}")
async def support_retry_failed_message(ticket_id:int,message_id:int,
                                      csrf_check=Depends(verify_csrf),
                                      user=Depends(get_current_user)):
    failed=await failed_support_message(ticket_id,message_id)
    if not failed:
        raise HTTPException(400,detail="Неудачное сообщение не найдено")
    sent=False
    try:
        from aiogram import Bot
        from bot.support_messages import format_staff_reply,reply_keyboard
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            delivered=await bot.send_message(failed["telegram_id"],
                failed["body"],
                parse_mode=None)
            tg_mid=delivered.message_id
        sent=True
    except Exception:
        import logging
        logging.exception("Retry support delivery failed for ticket %s",ticket_id)
    await mark_delivery(message_id,sent,tg_mid if sent else None)
    return RedirectResponse(f"/support/{ticket_id}?sent={'ok' if sent else 'fail'}",status_code=303)


@app.post("/support/{ticket_id}/status")
async def support_change_status(ticket_id:int,status:str=Form(...),
                                csrf_check=Depends(verify_csrf),
                                user=Depends(get_current_user)):
    try: uid=await set_ticket_status(ticket_id,status)
    except ValueError as exc: raise HTTPException(400,detail=str(exc))
    if status == "closed":
        try:
            from aiogram import Bot
            from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
            async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
                await bot.send_message(uid,
                    "✅ Предыдущий вопрос закрыт. Спасибо, что написали! "
                    "Если возникнут новые вопросы, просто отправьте сообщение сюда — "
                    "мы продолжим общение.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🛟 Помощь",callback_data="help_menu")
                    ]]))
        except Exception:
            import logging
            logging.exception("Support closure notice not delivered for %s",ticket_id)
    return RedirectResponse(f"/support/{ticket_id}?status=ok",status_code=303)



@app.post("/subscriptions/tribute-auto/checkout")
async def tribute_shared_checkout_change(
    enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    try:
        await set_shared_checkout_enabled(enabled, user["user"])
    except CheckoutNotReady as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return RedirectResponse(
        "/subscriptions/tribute-auto?changed=checkout", status_code=303,
    )


@app.post("/subscriptions/tribute-auto/removal")
async def tribute_paid_removal_change(
    enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    """Enable zero-grace kick only for verified Tribute 279348 paid members."""
    if enabled:
        # Enforce real Telegram ban permissions BEFORE flipping the DB flag.
        from aiogram import Bot
        from aiogram.enums import ChatMemberStatus
        config=await group_settings()
        if not config.get("chat_id"):
            raise HTTPException(400,detail="Закрытая группа не настроена")
        try:
            async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
                me=await bot.get_me()
                membership=await bot.get_chat_member(config["chat_id"],me.id)
                authorized=(
                    membership.status==ChatMemberStatus.CREATOR or
                    (membership.status==ChatMemberStatus.ADMINISTRATOR and
                     bool(getattr(membership,"can_restrict_members",False)))
                )
                if not authorized:
                    raise HTTPException(
                        400,
                        detail="Для исключения участников боту нужны права блокировать участников",
                    )
        except HTTPException:
            raise
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception("Unable to verify Telegram removal permissions")
            raise HTTPException(
                503,
                detail="Не удалось проверить права бота в закрытой группе",
            ) from exc
    try:
        await set_tribute_paid_removal_enabled(enabled, user["user"])
    except TributeRemovalError as exc:
        raise HTTPException(400,detail=str(exc)) from exc
    return RedirectResponse(
        "/subscriptions/tribute-auto?changed=removal",
        status_code=303,
    )


@app.get("/subscriptions/tribute-auto")
async def tribute_auto_page(request: Request, user=Depends(get_current_user)):
    state = await list_auto_settings()
    shared = await shared_checkout_status()
    paid_removal = await tribute_paid_removal_status()
    return templates.TemplateResponse(
        request=request, name="tribute_auto.html",
        context={
            **state,
            "shared": shared,
            "paid_removal": paid_removal,
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
        },
        headers={"Cache-Control":"no-store", "Referrer-Policy":"same-origin"},
    )


@app.post("/subscriptions/tribute-auto/setting")
async def tribute_auto_setting(
    enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user),
):
    try:
        await set_auto_processing_enabled(enabled, user["user"])
    except AutoPaymentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return RedirectResponse("/subscriptions/tribute-auto?changed=setting", status_code=303)


@app.post("/subscriptions/tribute-auto/offer")
async def tribute_auto_offer(
    subscription_id: int = Form(...),
    period_id: int = Form(...),
    enabled: bool = Form(False),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user),
):
    try:
        await set_auto_offer_enabled(
            subscription_id, period_id, enabled, user["user"]
        )
    except AutoPaymentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return RedirectResponse("/subscriptions/tribute-auto?changed=offer", status_code=303)


@app.post("/subscriptions/tribute-auto/add")
async def tribute_auto_add(
    subscription_id: int = Form(...), period_id: int = Form(...),
    channel_id: int = Form(...), amount_minor: int = Form(...),
    currency: str = Form(...), period: str = Form(...),
    plan_id: int = Form(...),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user),
):
    try:
        await register_auto_offer(
            subscription_id, period_id, channel_id, amount_minor,
            currency.lower(), period.lower(), plan_id, user["user"]
        )
    except AutoPaymentError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return RedirectResponse("/subscriptions/tribute-auto?changed=added", status_code=303)


@app.get("/subscriptions/tribute-test")
async def tribute_test_page(request: Request, user=Depends(get_current_user)):
    """Only signed-in admin sees test link history; no public test tariff."""
    return templates.TemplateResponse(
        request=request,
        name="tribute_test.html",
        context={
            "one_time_link": None,
            "test_links": await recent_tribute_test_links(),
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "test_subscription_id": TEST_SUBSCRIPTION_ID,
            "test_period_id": TEST_PERIOD_ID,
            "now": datetime.now(timezone.utc),
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"},
    )


@app.post("/subscriptions/tribute-test/create")
async def tribute_test_create(
    request: Request,
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    """Issue a 20-minute one-use bot entry, not a payment or an entitlement."""
    from aiogram import Bot

    if not os.environ.get("BOT_TOKEN"):
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            me = await telegram.get_me()
            username = me.username
    except Exception:
        raise HTTPException(status_code=503, detail="Cannot identify Telegram bot")
    if not username:
        raise HTTPException(status_code=503, detail="Telegram bot has no username")

    token = await create_tribute_test_link(user["user"])
    return templates.TemplateResponse(
        request=request,
        name="tribute_test.html",
        context={
            "one_time_link": f"https://t.me/{username}?start={token}",
            "test_links": await recent_tribute_test_links(),
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "test_subscription_id": TEST_SUBSCRIPTION_ID,
            "test_period_id": TEST_PERIOD_ID,
            "now": datetime.now(timezone.utc),
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "same-origin"},
    )


@app.post("/subscriptions/tribute-test/activate")
async def tribute_test_activate(
    event_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    """Approve a previously verified test payment; then offer an invite."""
    if confirm != "APPROVE_REAL_TRIBUTE_TEST_PAYMENT":
        raise HTTPException(400, detail="Подтвердите проверенную оплату")
    try:
        result = await activate_saved_test_purchase(event_id, user["user"])
    except TributeActivationError as exc:
        raise HTTPException(400, detail=str(exc)) from exc

    import logging
    from aiogram import Bot
    from bot.group_handlers import send_group_invitation
    invite_status = "failed"
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            invite_status = await send_group_invitation(
                telegram, result["telegram_id"], "tribute_verified_test_payment"
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "Paid user invitation failed uid=%s", result["telegram_id"]
        )
    return RedirectResponse(
        f'/subscriptions/users/{result["telegram_id"]}'
        f'?tribute_test=activated&invite={invite_status}',
        status_code=303,
    )


@app.get("/subscriptions/tribute-events")
async def tribute_webhook_journal(request: Request, user=Depends(get_current_user)):
    return templates.TemplateResponse(
        request=request, name="tribute_events.html",
        context={"events": await recent_events(100),
                 "webhook_ready": bool(os.environ.get("TRIBUTE_API_KEY")),
                 "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
                 "test_subscription_id": TEST_SUBSCRIPTION_ID,
                 "test_period_id": TEST_PERIOD_ID},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/subscriptions")
async def subscriptions_page(
    request: Request, search: str = "", page: int = 1, status: str = "all",
    payment_provider: str = "all", buyers_only: bool = False,
    tag: str = "", segment: str = "all",
    user=Depends(get_current_user),
):
    from urllib.parse import urlencode
    if status not in ("all","active","lifetime","expiring","expired","none"):
        status="all"
    summary, plans = await subscriptions_overview()
    payment_providers = await list_payment_providers()
    archived_payment_providers = await archived_providers()
    provider_counts = await payment_provider_counts()
    valid_providers={p["code"] for p in payment_providers}
    if payment_provider not in valid_providers:
        payment_provider="all"
    if payment_provider!="all":
        buyers_only=True
    tg_group = await group_settings()
    automation = await automation_settings()
    managed_total = await managed_count(tg_group.get('chat_id'))
    expiry_people = await expiry_overview()
    expiry_summary = await expiry_counts()
    users, total = await subscription_people(
        search, page, status, payment_provider, buyers_only, tag, segment
    )
    page = max(1, min(page, 100000))
    return templates.TemplateResponse(
        request=request, name="subscriptions.html",
        context={
            "summary": summary, "plans": plans, "users": users, "tg_group":tg_group,
            "payment_providers":payment_providers,"archived_payment_providers":archived_payment_providers,
            "provider_counts":provider_counts,
            "payment_provider_filter":payment_provider,"buyers_only":buyers_only,"access_filter":status,
            "crm_tag":tag,"crm_segment":segment,"crm_tags":await crm_tags(),
            "subscription_automation":automation,"managed_total":managed_total,
            "expiry_people":expiry_people,"expiry_summary":expiry_summary,
            "total": total, "search": search, "page": page,
            "pages": max(1, (total+29)//30),
            "next_url": "/subscriptions?" + urlencode({"search":search,"status":status,"payment_provider":payment_provider,"buyers_only":buyers_only,"tag":tag,"segment":segment,"page":page+1}),
            "prev_url": "/subscriptions?" + urlencode({"search":search,"status":status,"payment_provider":payment_provider,"buyers_only":buyers_only,"tag":tag,"segment":segment,"page":page-1}),
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
    )


@app.post("/subscriptions/provider/delete")
async def delete_payment_provider(
    code: str = Form(...), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "ARCHIVE_PAYMENT_PROVIDER":
        raise HTTPException(400,detail="Не подтверждено удаление")
    try:
        await archive_provider(code,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/subscriptions?provider=deleted#payment-providers",status_code=303)


@app.post("/subscriptions/provider/restore")
async def restore_payment_provider(
    code: str = Form(...), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "RESTORE_PAYMENT_PROVIDER":
        raise HTTPException(400,detail="Не подтверждено восстановление")
    try:
        await restore_provider(code,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/subscriptions?provider=restored#payment-providers",status_code=303)


@app.post("/subscriptions/provider/add")
async def add_payment_provider(
    code: str = Form(...), name: str = Form(...),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    try:
        await register_provider(code,name,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/subscriptions?provider=added#payment-providers",status_code=303)


@app.post("/subscriptions/provider")
async def update_payment_provider(
    code: str = Form(...), public_url: str = Form(""),
    notes: str = Form(""), enabled: bool = Form(False),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "SAVE_PAYMENT_PROVIDER":
        raise HTTPException(400, detail="Подтвердите настройки системы")
    try:
        await save_provider(code,public_url,notes,enabled,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse("/subscriptions?provider=saved#payment-providers",status_code=303)


@app.post("/subscriptions/automation")
async def subscriptions_automation_update(
    notifications_enabled: bool = Form(False),
    removal_enabled: bool = Form(False),
    grace_hours: int = Form(24),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    if confirm != "SAVE_AUTOMATION":
        raise HTTPException(400, detail="Подтвердите настройки автоматизации")
    if grace_hours not in (0,24,72):
        raise HTTPException(400, detail="Неверный льготный период")
    if removal_enabled:
        # First payment automation is intentionally grant-only. Do not let an
        # unrelated legacy admin control silently enable Telegram expulsions.
        if await is_auto_processing_enabled():
            raise HTTPException(
                400,
                detail="Сначала выключите автоматизацию оплаты Tribute; исключения участников проверяются отдельно.",
            )
        group = await group_settings()
        if not group.get("chat_id"):
            raise HTTPException(400, detail="Сначала подключите Telegram-группу")
        from aiogram import Bot
        from aiogram.enums import ChatMemberStatus
        try:
            async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
                me=await telegram.get_me()
                admin=await telegram.get_chat_member(group["chat_id"],me.id)
                if admin.status != ChatMemberStatus.CREATOR and not (
                    admin.status == ChatMemberStatus.ADMINISTRATOR and admin.can_restrict_members
                ):
                    raise HTTPException(400, detail="Боту нужны права блокировать участников")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400, detail="Не удалось проверить права бота")
    await save_automation_settings(
        notifications_enabled, removal_enabled, grace_hours, user["user"]
    )
    return RedirectResponse("/subscriptions?automation=saved",status_code=303)


@app.get("/subscriptions/users/{telegram_id}")
async def subscription_user_page(
    telegram_id: int, request: Request, user=Depends(get_current_user)
):
    data = await subscription_person(telegram_id)
    if data is None: raise HTTPException(404, detail="Пользователь не найден")
    participant, grants, subs, audit, selection = data
    tg_group = await group_settings()
    recent_invites = await latest_invites(telegram_id)
    can_invite = bool(await has_group_access(telegram_id))
    removal_test = await group_removal_test(telegram_id)
    removal_diagnostics = await member_removal_diagnostics(telegram_id, tg_group.get('chat_id'))
    # Read-only Telegram check: distinguish missing membership from bot API issues.
    removal_diagnostics["telegram_status"] = "не проверен"
    if tg_group.get("chat_id"):
        from aiogram import Bot
        try:
            async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
                me = await telegram.get_me()
                bot_member = await telegram.get_chat_member(tg_group["chat_id"], me.id)
                target_member = await telegram.get_chat_member(tg_group["chat_id"], telegram_id)
                removal_diagnostics["telegram_status"] = str(target_member.status)
                removal_diagnostics["bot_can_ban"] = (
                    str(bot_member.status) == "creator" or (
                        str(bot_member.status) == "administrator"
                        and bool(getattr(bot_member, "can_restrict_members", False))
                    )
                )
        except Exception as exc:
            removal_diagnostics["telegram_status"] = "Ошибка Telegram: " + str(exc)[:200]

    from zoneinfo import ZoneInfo
    tbilisi_tz = ZoneInfo("Asia/Tbilisi")
    direct_messages=await direct_message_history(telegram_id)
    return templates.TemplateResponse(
        request=request, name="subscription_user.html",
        context={
            "participant":participant, "grants":grants,
            "subscriptions":subs, "audit":audit, "selection":selection,
            "tg_group":tg_group, "recent_invites":recent_invites, "can_invite":can_invite,
            "removal_test":removal_test, "removal_diagnostics":removal_diagnostics,
            "tbilisi_tz":tbilisi_tz,
            "admin_profile":await get_subscriber_profile(telegram_id),
            "verified_payments":await subscriber_verified_payments(telegram_id),
            "access_summary":await subscriber_access_summary(telegram_id),
            "crm_timeline":await crm_timeline(telegram_id),
            "crm_tasks":await crm_tasks(uid=telegram_id,status="all",limit=100),
            "crm_messages":direct_messages,
            "crm_deletable_ids":{item["id"] for item in direct_messages
                if not item["deleted_at"] and item["status"]=="sent" and
                telegram_deletion_available(item["telegram_message_id"],item["finished_at"])},
            "message_request_key":__import__("secrets").token_hex(16),
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
    )


def ensure_telegram_message_deletable(message_id,created_at):
    """Bot API normally permits deletion for messages younger than 48 hours."""
    from datetime import datetime,timezone,timedelta
    if not message_id or not created_at:
        raise ValueError("У этого сообщения не сохранён Telegram ID — удалить его у пользователя невозможно")
    if datetime.now(timezone.utc)-created_at > timedelta(hours=47,minutes=55):
        raise ValueError("Telegram разрешает удалять сообщения только в течение 48 часов")


def telegram_deletion_available(message_id,created_at):
    from datetime import datetime,timezone,timedelta
    return bool(message_id and created_at and
                timedelta(0) <= datetime.now(timezone.utc)-created_at
                < timedelta(hours=47,minutes=55))


@app.post("/subscriptions/message/delete")
async def delete_subscriber_direct_message(
    telegram_id: int=Form(...),message_id: int=Form(...),
    confirm: str=Form(""),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    from aiogram import Bot
    import logging
    if confirm!="DELETE_BOTH" or telegram_id<=0 or message_id<=0:
        raise HTTPException(400,detail="Подтвердите удаление сообщения у пользователя")
    row=await direct_message_for_delete(telegram_id,message_id)
    if not row: raise HTTPException(404,detail="Сообщение не найдено")
    if row["deleted_at"]:
        return RedirectResponse(f"/subscriptions/users/{telegram_id}?delete=done#subscriber-direct",303)
    if row["status"]!="sent":
        raise HTTPException(400,detail="Сообщение не доставлено; удалить его из Telegram невозможно")
    try:
        ensure_telegram_message_deletable(row["telegram_message_id"],row["finished_at"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            await bot.delete_message(telegram_id,row["telegram_message_id"])
    except Exception as exc:
        logging.getLogger(__name__).warning("CRM Telegram deletion failed message=%s: %s",
                                             message_id,type(exc).__name__)
        return RedirectResponse(f"/subscriptions/users/{telegram_id}?delete=failed#subscriber-direct",303)
    await mark_direct_message_deleted(telegram_id,message_id,user["user"])
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?delete=done#subscriber-direct",303)


@app.post("/subscriptions/message")
async def send_subscriber_direct_message(
    telegram_id: int = Form(...), body: str = Form(...),
    request_key: str = Form(...), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    from aiogram import Bot
    import logging
    if confirm != "SEND_DIRECT" or not 0<telegram_id<=9999999999999:
        raise HTTPException(400,detail="Подтвердите получателя и отправку")
    try:
        record_id=await begin_direct_message(telegram_id,user["user"],body,request_key)
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    if record_id is None:
        return RedirectResponse(
            f"/subscriptions/users/{telegram_id}?message=duplicate#subscriber-direct",
            status_code=303
        )
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as bot:
            sent=await bot.send_message(
                telegram_id,body.strip(),parse_mode=None,
                disable_web_page_preview=True
            )
        await finish_direct_message(record_id,True,sent.message_id)
        result="sent"
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "CRM direct message not delivered uid=%s: %s",telegram_id,type(exc).__name__
        )
        await finish_direct_message(record_id,False,error=type(exc).__name__)
        result="failed"
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?message={result}#subscriber-direct",
        status_code=303
    )


@app.post("/subscriptions/history/clear")
async def clear_subscriber_crm_history(
    telegram_id: int=Form(...),confirm: str=Form(""),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user),
):
    if confirm!="CLEAR_CRM_HISTORY" or not 0<telegram_id<=9999999999999:
        raise HTTPException(400,detail="Подтвердите очистку истории")
    try:
        await clear_visible_crm_history(telegram_id,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?history=cleared#subscriber-timeline",
        status_code=303
    )


@app.post("/subscriptions/profile")
async def update_subscriber_profile(
    telegram_id: int=Form(...), note: str=Form(""), tags: str=Form(""),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user),
):
    if telegram_id<=0:
        raise HTTPException(400,detail="Некорректный Telegram ID")
    try:
        await save_subscriber_profile(telegram_id,note,tags,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?profile=saved#subscriber-notes",
        status_code=303
    )


@app.post("/subscriptions/grant")
async def subscriptions_grant(
    telegram_id: int = Form(...),
    grant_type: str = Form(...), end_date: str = Form(""),
    reason: str = Form(""), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    if confirm != "GRANT": raise HTTPException(400, detail="Подтвердите изменение доступа")
    expires = None
    if grant_type != "lifetime":
        try:
            dt=datetime.strptime(end_date,"%Y-%m-%d")
            expires=dt.replace(hour=23, minute=59, second=59,
                               tzinfo=ZoneInfo("Asia/Tbilisi")).astimezone(timezone.utc)
        except ValueError: raise HTTPException(400, detail="Укажите дату окончания")
    try:
        await grant_access(telegram_id,"community",grant_type,expires,reason,user["user"])
    except ValueError as exc: raise HTTPException(400, detail=str(exc))
    # Grant and invitation are separate: never undo a legitimate grant if
    # Telegram is temporarily unavailable or the participant blocked the bot.
    import logging
    from urllib.parse import quote
    from aiogram import Bot
    from bot.group_handlers import send_group_invitation
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            invite_status = await send_group_invitation(telegram, telegram_id, user["user"])
            if invite_status == "already_member":
                from zoneinfo import ZoneInfo
                if expires is None:
                    notice = "✅ Вам предоставлен постоянный доступ в «Круг Души». Вы уже состоите в группе."
                else:
                    date_label = expires.astimezone(ZoneInfo("Asia/Tbilisi")).strftime("%d.%m.%Y")
                    notice = (
                        "✅ Доступ в «Круг Души» подтверждён до "
                        + date_label + ". Вы уже состоите в группе."
                    )
                try:
                    await telegram.send_message(telegram_id, notice)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Grant confirmation not delivered for %s", telegram_id
                    )
                    invite_status = "message_failed"
    except Exception:
        logging.getLogger(__name__).exception("Invitation after grant failed for %s", telegram_id)
        invite_status = "failed"
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?invite={quote(invite_status)}", status_code=303
    )


@app.post("/subscriptions/renew")
async def subscriptions_renew(
    telegram_id: int = Form(...), days: int = Form(...),
    reason: str = Form(""), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    if confirm != "RENEW":
        raise HTTPException(400, detail="Подтвердите продление")
    try:
        new_expiry = await extend_community_access(telegram_id, days, reason, user["user"])
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    from .renewal_notice import notify_renewal
    notice = await notify_renewal(telegram_id, new_expiry, user["user"])
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?renewed=1&notice={notice}",status_code=303)


@app.post("/subscriptions/grant/expiry")
async def subscriptions_change_grant_expiry(
    telegram_id: int = Form(...), grant_id: int = Form(...),
    end_at: str = Form(...), confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    if confirm != "CHANGE_EXPIRY":
        raise HTTPException(400, detail="Подтвердите изменение срока")
    try:
        parsed=datetime.strptime(end_at,"%Y-%m-%dT%H:%M")
        expires=parsed.replace(tzinfo=ZoneInfo("Asia/Tbilisi")).astimezone(timezone.utc)
        await change_grant_expiry(telegram_id,grant_id,expires,user["user"])
    except ValueError as exc:
        raise HTTPException(400,detail=str(exc))
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?expiry=changed",status_code=303)


@app.post("/subscriptions/removal-test")
async def subscriptions_removal_test(
    telegram_id: int = Form(...), enabled: bool = Form(False),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    if confirm != "REMOVAL_TEST":
        raise HTTPException(400,detail="Подтвердите тестовый режим")
    if await subscription_person(telegram_id) is None:
        raise HTTPException(404,detail="Пользователь не найден")
    group=await group_settings()
    if not group.get("chat_id"):
        raise HTTPException(400,detail="Сначала подключите Telegram-группу")
    if enabled:
        from aiogram import Bot
        from aiogram.enums import ChatMemberStatus
        try:
            async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
                me=await telegram.get_me()
                admin=await telegram.get_chat_member(group["chat_id"],me.id)
                if admin.status != ChatMemberStatus.CREATOR and not (
                    admin.status == ChatMemberStatus.ADMINISTRATOR and admin.can_restrict_members
                ):
                    raise HTTPException(400,detail="Дайте боту право блокировать участников в настройках администратора группы")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(400,detail="Не удалось проверить права бота в Telegram-группе")
    await set_group_removal_test(telegram_id,group["chat_id"],enabled,user["user"])
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?test={'enabled' if enabled else 'disabled'}",status_code=303)


@app.post("/subscriptions/grants/history/clear")
async def subscriptions_clear_grant_history(
    telegram_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "CLEAR_REVOKED_GRANTS":
        raise HTTPException(400, detail="Подтвердите очистку истории")
    try:
        await clear_revoked_grant_history(telegram_id, user["user"])
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?grants=cleared", status_code=303
    )


@app.post("/subscriptions/invites/history/clear")
async def subscriptions_clear_invite_history(
    telegram_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "CLEAR_INVITE_HISTORY":
        raise HTTPException(400, detail="Подтвердите очистку приглашений")
    try:
        await clear_completed_invite_history(telegram_id, user["user"])
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?invites=cleared",
        status_code=303,
    )


@app.post("/subscriptions/audit/clear")
async def subscriptions_clear_member_audit(
    telegram_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "CLEAR_MEMBER_AUDIT":
        raise HTTPException(400, detail="Подтвердите очистку истории")
    try:
        await clear_visible_audit(telegram_id, user["user"])
    except ValueError as exc:
        raise HTTPException(404, detail=str(exc))
    return RedirectResponse(
        f"/subscriptions/users/{telegram_id}?audit=cleared", status_code=303
    )


@app.post("/subscriptions/revoke")
async def subscriptions_revoke(
    telegram_id: int = Form(...), grant_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    if confirm != "REVOKE": raise HTTPException(400,detail="Подтвердите отзыв")
    try: await revoke_grant(telegram_id,grant_id,user["user"])
    except ValueError as exc: raise HTTPException(400, detail=str(exc))
    from admin.revocation_removal import remove_after_revoke
    result = await remove_after_revoke(telegram_id)
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?revoke_result={result}",status_code=303)


@app.post("/subscriptions/plan")
async def subscriptions_plan(
    plan_id: int = Form(...), price: str = Form(""),
    payment_url: str = Form(""), active: bool = Form(False),
    csrf_check=Depends(verify_csrf),user=Depends(get_current_user)
):
    from decimal import Decimal, InvalidOperation
    try:
        amount=Decimal(price) if price.strip() else None
        if amount is not None and amount.as_tuple().exponent < -2:
            raise InvalidOperation()
        await update_plan(plan_id,amount,active,payment_url)
    except (InvalidOperation,ValueError) as exc:
        raise HTTPException(400, detail=str(exc) if isinstance(exc,ValueError) else "Неверная цена")
    return RedirectResponse("/subscriptions",status_code=303)


@app.post("/subscriptions/plan/delete")
async def subscriptions_plan_delete(
    plan_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
):
    if confirm != "DELETE_PLAN":
        raise HTTPException(400, detail="Подтвердите удаление тарифа")
    try:
        await archive_plan(plan_id,user["user"])
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse("/subscriptions?plan=deleted#subscription-plans",status_code=303)


@app.post("/subscriptions/plan/create")
async def subscriptions_plan_create(
    name: str = Form(""), price: str = Form(""),
    currency: str = Form("EUR"), duration_days: int = Form(30),
    is_lifetime: bool = Form(False),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user),
):
    from decimal import Decimal, InvalidOperation
    try:
        amount = Decimal(price.strip())
        await create_plan(name, amount, currency, duration_days, is_lifetime, user["user"])
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(400, detail=str(exc) if isinstance(exc, ValueError) else "Неверная цена")
    return RedirectResponse("/subscriptions", status_code=303)


@app.post("/subscriptions/group")
async def subscriptions_group_setup(
    chat_id: int = Form(...), csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    import os
    from aiogram import Bot
    from aiogram.enums import ChatMemberStatus
    if chat_id >= 0:
        raise HTTPException(400, detail="Укажите отрицательный ID закрытой супергруппы")
    async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
        try:
            chat = await telegram.get_chat(chat_id)
            me = await telegram.get_me()
            membership = await telegram.get_chat_member(chat_id, me.id)
        except Exception:
            raise HTTPException(400, detail="Группа недоступна. Проверьте ID и добавление бота")
        if chat.type != "supergroup":
            raise HTTPException(400, detail="Поддерживаются только Telegram-супергруппы")
        if membership.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            raise HTTPException(400, detail="Боту необходимы права администратора")
        if membership.status == ChatMemberStatus.ADMINISTRATOR and not membership.can_invite_users:
            raise HTTPException(400, detail="Включите боту право приглашать участников")
    await save_group(chat_id, chat.title or "Telegram-группа", user["user"])
    return RedirectResponse("/subscriptions?group=connected", status_code=303)


@app.post("/subscriptions/invite")
async def subscriptions_send_invite(
    telegram_id: int = Form(...),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    from aiogram import Bot
    from bot.group_handlers import send_group_invitation
    if await subscription_person(telegram_id) is None:
        raise HTTPException(404, detail="Пользователь не найден")
    try:
        async with Bot(token=os.environ["BOT_TOKEN"]) as telegram:
            result = await send_group_invitation(telegram, telegram_id, user["user"])
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Manual invite failure for %s", telegram_id)
        result = "failed"
    return RedirectResponse(f"/subscriptions/users/{telegram_id}?invite={result}", status_code=303)



@app.get("/broadcast")
async def broadcast_page(request: Request, user=Depends(get_current_user)):
    counts = {name: await count_recipients(name) for name in ("started", "accepted", "clicked")}
    campaigns = await list_broadcasts()
    return templates.TemplateResponse(
        request=request, name="broadcast.html",
        context={
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "counts": counts, "campaigns": campaigns, "preview": None,
            "templates": await list_message_templates(), "rules": await rules(), "triggers": TRIGGERS,
        }, headers={"Cache-Control": "no-store"}
    )


@app.post("/broadcast/preview")
async def preview_broadcast(
    request: Request,
    body: str = Form(""),
    audience: str = Form("accepted"),
    button_text: str = Form(""),
    button_url: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    if audience not in AUDIENCES or not (1 <= len(body.strip()) <= 4000):
        raise HTTPException(status_code=400, detail="Введите сообщение длиной от 1 до 4000 символов и выберите аудиторию")
    from urllib.parse import urlparse
    button_text, button_url = button_text.strip(), button_url.strip()
    if bool(button_text) != bool(button_url) or len(button_text) > 64:
        raise HTTPException(400, detail="Для кнопки укажите название и ссылку")
    if button_url and (urlparse(button_url).scheme not in ("https", "http") or not urlparse(button_url).netloc):
        raise HTTPException(400, detail="Неверная ссылка кнопки")
    counts = {name: await count_recipients(name) for name in ("started", "accepted", "clicked")}
    return templates.TemplateResponse(
        request=request, name="broadcast.html",
        context={
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "counts": counts, "campaigns": await list_broadcasts(),
            "templates": await list_message_templates(), "rules": await rules(), "triggers": TRIGGERS,
            "preview": {"body": body.strip(), "audience": audience,
                        "recipients": counts[audience], "button_text": button_text, "button_url": button_url},
        }, headers={"Cache-Control": "no-store"}
    )


@app.post("/broadcast/template")
async def save_template(
    template_id: int = Form(0), title: str = Form(""), body: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    try:
        await upsert_template(template_id, title, body)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse("/broadcast#broadcast-templates", status_code=303)


@app.post("/broadcast/rule")
async def save_rule(
    trigger: str = Form(""), enabled: bool = Form(False), body: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    try:
        await set_rule(trigger, enabled, body)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse("/broadcast#broadcast-automation", status_code=303)


@app.post("/broadcast/send")
async def send_broadcast(
    request: Request,
    body: str = Form(""),
    audience: str = Form(""),
    confirm: str = Form(""),
    button_text: str = Form(""),
    button_url: str = Form(""),
    schedule_at: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    if confirm != "SEND" or audience not in AUDIENCES or not (1 <= len(body.strip()) <= 4000):
        raise HTTPException(status_code=400, detail="Подтверждение отправки отсутствует")
    from urllib.parse import urlparse
    button_text, button_url = button_text.strip(), button_url.strip()
    if bool(button_text) != bool(button_url) or len(button_text) > 64:
        raise HTTPException(400, detail="Для кнопки укажите название и ссылку")
    if button_url and (urlparse(button_url).scheme not in ("https", "http") or not urlparse(button_url).netloc):
        raise HTTPException(400, detail="Неверная ссылка кнопки")
    from datetime import datetime, timezone, timedelta
    from zoneinfo import ZoneInfo
    scheduled_at = None
    if schedule_at.strip():
        try:
            local_dt = datetime.fromisoformat(schedule_at)
            if local_dt.tzinfo is not None:
                raise ValueError()
            scheduled_at = local_dt.replace(tzinfo=ZoneInfo("Asia/Tbilisi")).astimezone(timezone.utc)
            if scheduled_at < datetime.now(timezone.utc) + timedelta(minutes=2):
                raise ValueError()
        except ValueError:
            raise HTTPException(400, detail="Укажите время Тбилиси минимум через 2 минуты")
    await create_broadcast(body, audience, scheduled_at=scheduled_at, button_text=button_text, button_url=button_url)
    return RedirectResponse("/broadcast", status_code=303)


# ⚙️ UPDATE SETTINGS
@app.post("/update")
async def update_settings(
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
    video_url: str = Form(""),
    text1: str = Form(""),
    text2: str = Form(""),
    btn1_text: str = Form(...),
    btn1_url: str = Form(...),
    btn2_text: str = Form(...),
    btn2_url: str = Form(...)
):
    async with SessionLocal() as session:
        await session.execute(
            text("""
                UPDATE settings
                SET
                    video_url = :video_url,
                    text1 = :text1,
                    text2 = :text2,
                    btn1_text = :btn1_text,
                    btn1_url = :btn1_url,
                    btn2_text = :btn2_text,
                    btn2_url = :btn2_url
                WHERE id = 1
            """),
            {
                "video_url": video_url,
                "text1": text1,
                "text2": text2,
                "btn1_text": btn1_text,
                "btn1_url": btn1_url,
                "btn2_text": btn2_text,
                "btn2_url": btn2_url
            }
        )

        await session.commit()

    return RedirectResponse(
        url="/settings/bot?saved=1",
        status_code=303
    )



# Subscription onboarding configuration (CSRF + login protected)
@app.post("/update-flow")
async def update_flow(
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
    video_note_file_id: str = Form(""),
    offer_text: str = Form(""),
    offer_url: str = Form(""),
    offer_version: str = Form("1"),
    subscription_text: str = Form(""),
    subscription_url: str = Form("")
):
    from urllib.parse import urlparse
    from fastapi import HTTPException

    def clean_url(value):
        value = value.strip()
        if value:
            parsed = urlparse(value)
            if parsed.scheme not in ("https", "http") or not parsed.netloc:
                raise HTTPException(status_code=400, detail="Invalid URL")
        return value

    version = offer_version.strip()
    if not version or len(version) > 100:
        raise HTTPException(status_code=400, detail="Invalid offer version")

    await save_flow_settings({
        "video_note_file_id": video_note_file_id.strip(),
        "offer_text": offer_text.strip(),
        "offer_url": clean_url(offer_url),
        "offer_version": version,
        "subscription_text": subscription_text.strip(),
        "subscription_url": clean_url(subscription_url)
    })
    return RedirectResponse(url="/settings/bot?saved=1", status_code=303)


# 🔐 CHANGE CREDENTIALS
@app.post("/change-credentials")
async def change_credentials(
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user),
    new_username: str = Form(...),
    new_password: str = Form(...)
):
    if not new_username or not new_password:
        raise HTTPException(
            status_code=400,
            detail="Username and password required"
        )

    if len(new_password) < 4:
        raise HTTPException(
            status_code=400,
            detail="Password too short"
        )

    hashed_password = hash_password(new_password)

    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM admins WHERE username = :u"),
            {"u": new_username}
        )

        existing_user = result.fetchone()

        if existing_user:
            existing_user = existing_user._mapping

            if existing_user["username"] != user["user"]:
                raise HTTPException(
                    status_code=400,
                    detail="Username already exists"
                )

        await session.execute(
            text("""
                UPDATE admins
                SET
                    username = :new_username,
                    password = :new_password
                WHERE username = :old_username
            """),
            {
                "new_username": new_username,
                "new_password": hashed_password,
                "old_username": user["user"]
            }
        )

        await session.commit()

    response = RedirectResponse(
        url="/login",
        status_code=302
    )

    response.delete_cookie("access_token")

    return response


# 🔌 WEBSOCKET
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    origin = websocket.headers.get("origin")

    if origin != "https://bot.arutyun.me":
        await websocket.close(code=1008)
        return

    token = websocket.cookies.get("access_token")

    if not token:
        await websocket.close(code=1008)
        return

    try:
        payload = decode_token(token)
        username = payload.get("user")

        if not username:
            raise ValueError("Invalid user")

        async with SessionLocal() as session:
            result = await session.execute(
                text("SELECT username FROM admins WHERE username=:u"),
                {"u": username}
            )

            if not result.fetchone():
                raise ValueError("Unknown user")

    except Exception:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    connections.append(websocket)

    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        if websocket in connections:
            connections.remove(websocket)