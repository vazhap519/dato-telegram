
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
from db.expiry_monitor import initialize_expiry_monitor, expiry_overview, expiry_counts
from db.subscription_automation import initialize_subscription_automation, automation_settings, save_automation_settings, managed_count, member_removal_diagnostics
from db.group_access import initialize_group_access, group_settings, save_group, has_group_access, record_invite, latest_invites, group_removal_test, set_group_removal_test
from db.subscriptions import initialize_subscriptions, overview as subscriptions_overview, people as subscription_people, person as subscription_person, grant_access, revoke_grant, update_plan, create_plan, change_grant_expiry, extend_community_access
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
    await initialize_broadcasts()
    await initialize_automation()
    await initialize_subscriptions()
    await initialize_group_access()
    await initialize_expiry_monitor()
    await initialize_subscription_automation()


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
        "/subscriptions/grant",
        "/subscriptions/revoke",
        "/subscriptions/grant/expiry",
        "/subscriptions/renew",
        "/subscriptions/automation",
        "/subscriptions/removal-test",
        "/subscriptions/plan",
        "/subscriptions/plan/create",
        "/subscriptions/group",
        "/subscriptions/invite",
        "/change-credentials",
    }

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
async def dashboard(
    request: Request,
    user=Depends(get_current_user)
):
    count = await get_users_count()
    settings = await get_settings()
    flow = await get_flow_settings()
    stats = await registration_stats()
    csrf_token = make_csrf_token(
        request.cookies.get("access_token", "")
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "count": count,
            "settings": settings,
            "flow": flow,
            "stats": stats,
            "user": user,
            "csrf_token": csrf_token
        }
    )


@app.post("/create-video-upload")
async def create_video_upload(request: Request, csrf_check=Depends(verify_csrf), user=Depends(get_current_user)):
    import secrets
    code = secrets.token_urlsafe(24)
    await create_upload_code(code)
    settings = await get_settings()
    flow = await get_flow_settings()
    stats = await registration_stats()
    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "count": stats["total"], "settings": settings, "flow": flow,
            "stats": stats, "user": user,
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "upload_command": "/upload " + code
        },
        headers={"Cache-Control": "no-store"}
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
@app.get("/subscriptions")
async def subscriptions_page(
    request: Request, search: str = "", page: int = 1,
    user=Depends(get_current_user),
):
    from urllib.parse import urlencode
    summary, plans = await subscriptions_overview()
    tg_group = await group_settings()
    automation = await automation_settings()
    managed_total = await managed_count(tg_group.get('chat_id'))
    expiry_people = await expiry_overview()
    expiry_summary = await expiry_counts()
    users, total = await subscription_people(search, page)
    page = max(1, min(page, 100000))
    return templates.TemplateResponse(
        request=request, name="subscriptions.html",
        context={
            "summary": summary, "plans": plans, "users": users, "tg_group":tg_group,
            "subscription_automation":automation,"managed_total":managed_total,
            "expiry_people":expiry_people,"expiry_summary":expiry_summary,
            "total": total, "search": search, "page": page,
            "pages": max(1, (total+29)//30),
            "next_url": "/subscriptions?" + urlencode({"search":search,"page":page+1}),
            "prev_url": "/subscriptions?" + urlencode({"search":search,"page":page-1}),
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
    )


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
    return templates.TemplateResponse(
        request=request, name="subscription_user.html",
        context={
            "participant":participant, "grants":grants,
            "subscriptions":subs, "audit":audit, "selection":selection,
            "tg_group":tg_group, "recent_invites":recent_invites, "can_invite":can_invite,
            "removal_test":removal_test, "removal_diagnostics":removal_diagnostics,
            "tbilisi_tz":tbilisi_tz,
            "csrf_token": make_csrf_token(request.cookies.get("access_token","")),
        }, headers={"Cache-Control":"no-store"}
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


@app.post("/subscriptions/revoke")
async def subscriptions_revoke(
    telegram_id: int = Form(...), grant_id: int = Form(...),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    if confirm != "REVOKE": raise HTTPException(400,detail="Подтвердите отзыв")
    try: await revoke_grant(telegram_id,grant_id,user["user"])
    except ValueError as exc: raise HTTPException(400, detail=str(exc))
    return RedirectResponse(f"/subscriptions/users/{telegram_id}",status_code=303)


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
    return RedirectResponse("/broadcast", status_code=303)


@app.post("/broadcast/rule")
async def save_rule(
    trigger: str = Form(""), enabled: bool = Form(False), body: str = Form(""),
    csrf_check=Depends(verify_csrf), user=Depends(get_current_user)
):
    try:
        await set_rule(trigger, enabled, body)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse("/broadcast", status_code=303)


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
        url="/dashboard",
        status_code=302
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
    return RedirectResponse(url="/dashboard", status_code=303)


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