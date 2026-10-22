
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
from db.broadcast import initialize_broadcasts, count_recipients, create_broadcast, list_broadcasts, AUDIENCES
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


# Protection against cross-origin admin requests
@app.middleware("http")
async def protect_admin_forms(request: Request, call_next):
    protected_paths = {
        "/update",
        "/update-flow",
        "/create-video-upload",
        "/broadcast/preview",
        "/broadcast/send",
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


@app.get("/broadcast")
async def broadcast_page(request: Request, user=Depends(get_current_user)):
    counts = {name: await count_recipients(name) for name in ("started", "accepted", "clicked")}
    campaigns = await list_broadcasts()
    return templates.TemplateResponse(
        request=request, name="broadcast.html",
        context={
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "counts": counts, "campaigns": campaigns, "preview": None,
        }, headers={"Cache-Control": "no-store"}
    )


@app.post("/broadcast/preview")
async def preview_broadcast(
    request: Request,
    body: str = Form(""),
    audience: str = Form("accepted"),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    if audience not in AUDIENCES or not (1 <= len(body.strip()) <= 4000):
        raise HTTPException(status_code=400, detail="Введите сообщение длиной от 1 до 4000 символов и выберите аудиторию")
    counts = {name: await count_recipients(name) for name in ("started", "accepted", "clicked")}
    return templates.TemplateResponse(
        request=request, name="broadcast.html",
        context={
            "csrf_token": make_csrf_token(request.cookies.get("access_token", "")),
            "counts": counts, "campaigns": await list_broadcasts(),
            "preview": {"body": body.strip(), "audience": audience,
                        "recipients": counts[audience]},
        }, headers={"Cache-Control": "no-store"}
    )


@app.post("/broadcast/send")
async def send_broadcast(
    request: Request,
    body: str = Form(""),
    audience: str = Form(""),
    confirm: str = Form(""),
    csrf_check=Depends(verify_csrf),
    user=Depends(get_current_user)
):
    if confirm != "SEND" or audience not in AUDIENCES or not (1 <= len(body.strip()) <= 4000):
        raise HTTPException(status_code=400, detail="Подтверждение отправки отсутствует")
    await create_broadcast(body, audience)
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