from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, BigInteger, Text, TIMESTAMP, func

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True)
    created_at = Column(TIMESTAMP, server_default=func.now())

class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True)
    video_url = Column(Text)
    text1 = Column(Text)
    text2 = Column(Text)
    btn1_text = Column(Text)
    btn1_url = Column(Text)
    btn2_text = Column(Text)
    btn2_url = Column(Text)

class Admin(Base):
    __tablename__ = "admins"
    id = Column(Integer, primary_key=True)
    username = Column(Text, unique=True)
    password = Column(Text)