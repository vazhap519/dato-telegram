from datetime import datetime, timedelta, timezone
from passlib.context import CryptContext
from jose import jwt
import os
from dotenv import load_dotenv

load_dotenv()

SECRET = os.environ["SECRET_KEY"]

pwd = CryptContext(
    schemes=["pbkdf2_sha256"],  # 🔥 სტაბილური
    deprecated="auto"
)

def hash_password(p):
    return pwd.hash(p)

def verify_password(p, h):
    return pwd.verify(p, h)

def create_token(data: dict):
    payload = data.copy()
    payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=12)
    return jwt.encode(payload, SECRET, algorithm="HS256")

def decode_token(token: str):
    return jwt.decode(token, SECRET, algorithms=["HS256"])
