#!/usr/bin/env python3
"""Stage analytics release on inactive 8082, leaving public 8081 untouched."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
PUBLIC="121b4ef32ec4bff530781df415cc966ced99ab12"
STANDBY="f7ea20273c15e170bb58f8eaa97341e84974ec77"
NEW="800b69ffa6790184467800ad40d0bef0174e0b64"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()
def response(url):
    try:
        with urlopen(Request(url),timeout=8) as r:return r.status
    except HTTPError as e:return e.code

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree dirty")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned commit missing")
    if not (RELEASES/NEW/".venv/.install-ok").is_file():
        raise RuntimeError("First run prepare_miniapp_analytics.py")
    sys.path.insert(0,str(RELEASES/PUBLIC/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8081:
            raise RuntimeError("Unexpected Nginx active port")
        if bg.release_sha_for_port(8081)!=PUBLIC or bg.release_sha_for_port(8082)!=STANDBY:
            raise RuntimeError("Unexpected live and standby revisions")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Admin health failed before staging")
        if response("https://bot.arutyun.me/tg-support/analytics")!=404:
            raise RuntimeError("Analytics URL is already public; investigate first")
        pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        botdir=run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
        if int(pid)<1 or botdir!=str(RELEASES/BOT):
            raise RuntimeError("Bot changed or inactive")
        print("STAGING ANALYTICS RELEASE ON INACTIVE 8082",flush=True)
        bg.stage(NEW,allow_bot_changes=True,reviewed_support=True)
        if bg.release_sha_for_port(8082)!=NEW or not bg.health(8082):
            raise RuntimeError("New analytics standby failed health check")
        if response("http://127.0.0.1:8082/tg-support/analytics")!=200:
            raise RuntimeError("Analytics HTML endpoint unavailable")
        for path in ("/tg-support/api/analytics",
                     "/tg-support/api/analytics?period=all",
                     "/tg-support/api/members",
                     "/tg-support/api/members/1"):
            if response("http://127.0.0.1:8082"+path)!=401:
                raise RuntimeError("Expected signed Telegram guard on "+path)
        if response("http://127.0.0.1:8082/login")!=200:
            raise RuntimeError("Login route unexpectedly changed")
        if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081):
            raise RuntimeError("Public admin changed while staging")
        if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=pid:
            raise RuntimeError("Bot PID changed while staging")
        print("ANALYTICS STAGED: 8082 =",NEW[:12],flush=True)
        print("PUBLIC UNCHANGED: 8081 =",PUBLIC[:12],flush=True)
        print("BOT UNCHANGED PID:",pid,flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("ANALYTICS STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
