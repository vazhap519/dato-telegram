#!/usr/bin/env python3
"""Stage reviewed member cards/actions release in inactive admin 8081.

Public Nginx continues to use 8082. The bot stays on its current
pinned release. The reviewed Blue/Green stage helper restores the previous
standby unit on startup failure. No database migration.
"""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
CANDIDATE="121b4ef32ec4bff530781df415cc966ced99ab12"
PUBLIC="f7ea20273c15e170bb58f8eaa97341e84974ec77"
BACKUP="2d609a3267261e2370e01e02596916d1bdc044c6"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()

def code(url,method="GET"):
    try:
        with urlopen(Request(url,method=method),timeout=8) as result:
            return result.status
    except HTTPError as exc:return exc.code

def main():
    if os.geteuid()!=0:raise RuntimeError("Requires root")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Git working tree modified")
    if run("git","-C",str(ROOT),"rev-parse","--verify",CANDIDATE)!=CANDIDATE:
        raise RuntimeError("Pinned candidate commit unavailable")
    release=RELEASES/CANDIDATE
    if not (release/".venv/.install-ok").exists():
        raise RuntimeError("Prepare isolated candidate before staging")
    sys.path.insert(0,str(RELEASES/PUBLIC/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.target_commit=lambda:CANDIDATE
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Public must be on 8082")
        if bg.release_sha_for_port(8082)!=PUBLIC:
            raise RuntimeError("Public release changed")
        if bg.release_sha_for_port(8081)!=BACKUP:
            raise RuntimeError("Backup release unexpectedly changed")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Admin slot unhealthy before stage")
        if code("https://bot.arutyun.me/tg-support/members")!=200:
            raise RuntimeError("Current public members page unhealthy")
        botpid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        botdir=run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
        if int(botpid)<1 or botdir!=str(RELEASES/BACKUP):
            raise RuntimeError("Current bot unexpectedly changed")
        print("STAGING REVIEWED MEMBER ACTIONS TO 8081:",CANDIDATE[:12],flush=True)
        # Earlier support-layer schema changes are already reviewed in the
        # running release. Do not deploy any bot or DB changes here.
        bg.stage(CANDIDATE,allow_bot_changes=True,reviewed_support=True)
        if bg.release_sha_for_port(8081)!=CANDIDATE or not bg.health(8081):
            raise RuntimeError("New standby failed basic health")
        if code("http://127.0.0.1:8081/tg-support/members")!=200:
            raise RuntimeError("Member cards page not available on new standby")
        for path in ("/tg-support/api/members","/tg-support/api/members/1"):
            if code("http://127.0.0.1:8081"+path)!=401:
                raise RuntimeError("Signed Telegram API authorization failed: "+path)
        if code("http://127.0.0.1:8081/tg-support/api/members/1/grant",method="POST")!=401:
            raise RuntimeError("Owner action endpoint does not enforce Telegram auth")
        if bg.routing(bg.nginx_path().read_text())!=8082 or not bg.health(8082):
            raise RuntimeError("Public admin changed during standby stage")
        if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=botpid:
            raise RuntimeError("Bot PID changed during standby stage")
        print("MEMBER ACTIONS STAGED: standby 8081",CANDIDATE[:12],flush=True)
        print("PUBLIC UNCHANGED: 8082",PUBLIC[:12],flush=True)
        print("BOT UNCHANGED PID:",botpid,flush=True)
        print("Do not cut over until live owner/admin UI and permission tests are reviewed.")

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("MEMBER ACTIONS STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:320],file=sys.stderr)
        sys.exit(1)
