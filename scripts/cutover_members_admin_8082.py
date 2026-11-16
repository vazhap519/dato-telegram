#!/usr/bin/env python3
"""Pinned read-only public cutover preflight for Members Mini App.

Default: read-only. With --cutover: switch *only* Nginx 8081 -> 8082
using reviewed Blue/Green and verify endpoints; restore 8081 on any
post-switch error. Bot and DB are never restarted or modified.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

RELEASES=Path("/opt/dato-admin-releases")
ROOT=Path("/opt/dato-telegram")
ACTIVE="2d609a3267261e2370e01e02596916d1bdc044c6"
NEW="f7ea20273c15e170bb58f8eaa97341e84974ec77"

def status(url):
    try:
        with urlopen(url,timeout=8) as response:
            return response.status
    except HTTPError as exc:
        return exc.code

def run(*args):
    return subprocess.check_output(args,timeout=20,text=True).strip()

def verify(bluegreen):
    if bluegreen.routing(bluegreen.nginx_path().read_text())!=8081:
        raise RuntimeError("Public Nginx is not on 8081")
    if bluegreen.release_sha_for_port(8081)!=ACTIVE or bluegreen.release_sha_for_port(8082)!=NEW:
        raise RuntimeError("Expected pinned admin releases are not on 8081/8082")
    for port in (8081,8082):
        if run("systemctl","is-active",bluegreen.UNITS[port])!="active":
            raise RuntimeError(f"Admin unit on {port} inactive")
        if not bluegreen.health(port):
            raise RuntimeError(f"Admin health failed on {port}")
    expected={
        "/login":200,
        "/tg-support":200,
        "/tg-support/members":200,
        "/tg-support/api/me":401,
        "/tg-support/api/members":401,
        "/tg-support/api/members/1":401,
        "/staff":401,
        "/support":401,
    }
    for path,needed in expected.items():
        code=status("http://127.0.0.1:8082"+path)
        if code!=needed:
            raise RuntimeError(f"Standby {path}: HTTP {code}, expected {needed}")
    if status("http://127.0.0.1:8081/tg-support/api/members")!=404:
        raise RuntimeError("Unexpected members endpoint in active admin")
    # Public /login is intentionally protected by Nginx Basic Auth.
    # Internal /login is expected to be HTTP 200, checked above by bg.health.
    # Reject HTTP 200 here: it would indicate public Basic Auth was removed.
    public_expected={"/login":401,"/tg-support":200}
    for path,expected in public_expected.items():
        received=status("https://bot.arutyun.me"+path)
        if received!=expected:
            raise RuntimeError(f"Public {path}: HTTP {received}, expected {expected}")
    pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if int(pid)<=0 or run("systemctl","is-active","dato-telegram-bot.service")!="active":
        raise RuntimeError("Bot not healthy")
    if run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")!=str(RELEASES/ACTIVE):
        raise RuntimeError("Bot release unexpectedly changed")
    from collections import Counter
    cmd=run("ps","-eo","pid=,args=")
    pollers=[int(line.strip().split(maxsplit=1)[0]) for line in cmd.splitlines()
             if " -m scripts.bot_runner" in line and not "python - " in line]
    if pollers!=[int(pid)]:
        raise RuntimeError("Expected exactly one supervised polling bot")
    return pid

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cutover",action="store_true",
                        help="Explicitly switch public Nginx to staged release")
    opts=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree modified")
    for sha in (ACTIVE,NEW):
        if run("git","-C",str(ROOT),"rev-parse","--verify",sha)!=sha:
            raise RuntimeError("Missing pinned commit: "+sha)
        if not (RELEASES/sha/".venv/bin/python").is_file():
            raise RuntimeError("Pinned admin environment missing: "+sha)
    sys.path.insert(0,str(RELEASES/ACTIVE/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        bot_pid=verify(bg)
        print("MEMBERS CUTOVER PRECHECK OK: public=8081, staged=8082, bot PID="+bot_pid,flush=True)
        if not opts.cutover:
            print("READ-ONLY COMPLETE. Public routing is unchanged.")
            print("Next: manual owner/admin and support-403 check before --cutover.")
            return
        print("Switching ONLY Nginx: 8081 -> 8082",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("Public route did not switch to 8082")
            for port,sha in ((8081,ACTIVE),(8082,NEW)):
                if bg.release_sha_for_port(port)!=sha or not bg.health(port):
                    raise RuntimeError(f"Admin on {port} changed/failed")
            if status("https://bot.arutyun.me/login")!=401:
                raise RuntimeError("Public /login Basic Auth protection changed")
            for path,need in (("/tg-support/members",200),
                              ("/tg-support/api/members",401),
                              ("/tg-support/api/members/1",401),
                              ("/tg-support",200)):
                got=status("https://bot.arutyun.me"+path)
                if got!=need:
                    raise RuntimeError(f"Public {path}: {got} expected {need}")
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=bot_pid:
                raise RuntimeError("Bot PID changed during Nginx-only switch")
            print("MEMBERS PUBLIC CUTOVER VERIFIED: public 8082, rollback 8081; bot unchanged.")
            print("Manual Telegram owner/admin login and real member data still need verification.")
        except Exception:
            print("Cutover validation failed: restoring public route to 8081",file=sys.stderr)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("CRITICAL: Automatic Nginx rollback did not restore 8081")
            print("Previous public version restored to 8081.",file=sys.stderr)
            raise

if __name__=="__main__":
    try:
        main()
    except Exception as exc:
        print("MEMBERS CUTOVER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:300],file=sys.stderr)
        sys.exit(1)
