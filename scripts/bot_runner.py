#!/usr/bin/env python3
"""Launch bot from immutable release, using the existing shared secret file.

Systemd ExecStart runs exactly one polling process, no second bot instance.
"""
import os
from pathlib import Path
import runpy
from dotenv import load_dotenv

ROOT = Path("/opt/dato-telegram")
release = Path.cwd().resolve()
if not (release / "bot/main.py").is_file():
    raise SystemExit("Release does not contain bot/main.py")
if not load_dotenv(ROOT / ".env"):
    raise SystemExit("Bot environment file missing or empty")
if not os.environ.get("BOT_TOKEN"):
    raise SystemExit("BOT_TOKEN missing")
runpy.run_module("bot.main",run_name="__main__",alter_sys=True)
