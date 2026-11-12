#!/usr/bin/env python3
"""Start exactly one ASGI admin server in an immutable release directory.

Used only by blue/green admin slot units. No Telegram bot polling.
"""
import argparse
import os
from pathlib import Path

from dotenv import dotenv_values
import uvicorn


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port",type=int,choices=[8081,8082],required=True)
    args=parser.parse_args()
    release=Path.cwd()
    if not (release/"admin/main.py").is_file():
        raise SystemExit("Working directory is not a release")
    env=Path("/opt/dato-telegram/.env")
    if not env.is_file():
        raise SystemExit("Shared .env missing")
    for key,value in dotenv_values(env).items():
        if value is not None:
            os.environ[key]=value
    uvicorn.run("admin.main:app",host="127.0.0.1",port=args.port,workers=1,
                log_level="info")


if __name__=="__main__":
    main()
