from passlib.context import CryptContext
from jose import jwt
import os

SECRET = os.getenv("SECRET_KEY", "supersecret")

pwd = CryptContext(
    schemes=["pbkdf2_sha256"],  # 🔥 სტაბილური
    deprecated="auto"
)

def hash_password(p):
    return pwd.hash(p)

def verify_password(p, h):
    return pwd.verify(p, h)

def create_token(data: dict):
    return jwt.encode(data, SECRET, algorithm="HS256")

def decode_token(token: str):
    return jwt.decode(token, SECRET, algorithms=["HS256"])