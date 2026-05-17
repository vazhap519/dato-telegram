from fastapi import FastAPI, Request, Form, Depends, Cookie, HTTPException, WebSocket
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from db.database import SessionLocal
from db.queries import get_users_count, get_settings
from .auth import verify_password, create_token, decode_token, hash_password
from .websocket import connections

app = FastAPI()
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
    return templates.TemplateResponse("login.html", {"request": request})


# 🔐 LOGIN LOGIC
@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    async with SessionLocal() as session:
        result = await session.execute(
            text("SELECT * FROM admins WHERE username=:u"),
            {"u": username}
        )
        user = result.fetchone()

    if not user or not verify_password(password, user.password):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Wrong credentials"
        })

    token = create_token({"user": username})

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie("access_token", token, httponly=True)

    return response


# 📊 DASHBOARD
@app.get("/dashboard")
async def dashboard(request: Request, user=Depends(get_current_user)):
    count = await get_users_count()
    settings = await get_settings()

    return templates.TemplateResponse("index.html", {
        "request": request,
        "count": count,
        "settings": settings,
        "user": user
    })


# ⚙️ UPDATE SETTINGS
@app.post("/update")
async def update_settings(
    user=Depends(get_current_user),
    video_url: str = Form(...),
    text1: str = Form(...),
    text2: str = Form(...),
    btn1_text: str = Form(...),
    btn1_url: str = Form(...),
    btn2_text: str = Form(...),
    btn2_url: str = Form(...)
):
    async with SessionLocal() as session:
        await session.execute(
            text("""
                UPDATE settings SET
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

    return RedirectResponse(url="/dashboard", status_code=302)


# 🔐 CHANGE CREDENTIALS
@app.post("/change-credentials")
async def change_credentials(
    user=Depends(get_current_user),
    new_username: str = Form(...),
    new_password: str = Form(...)
):
    if not new_username or not new_password:
        raise HTTPException(status_code=400, detail="Username and password required")

    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password too short")

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
                raise HTTPException(status_code=400, detail="Username already exists")

        await session.execute(
            text("""
                UPDATE admins
                SET username = :new_username,
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

    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("access_token")

    return response


# 🔌 WEBSOCKET
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        if websocket in connections:
            connections.remove(websocket)