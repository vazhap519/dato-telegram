#!/usr/bin/env python3
"""Pinned Nginx-only member cards and actions cutover with rollback.

Default is read-only. Explicit --cutover switches 8082 -> 8081 and
verifies the *new UI marker*, because both releases serve the same URL
with HTTP 200. The bot and database are not changed.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
PUBLIC="f7ea20273c15e170bb58f8eaa97341e84974ec77"
NEW="121b4ef32ec4bff530781df415cc966ced99ab12"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()

def response(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as item:
            return item.status,item.read(250000).decode("utf-8",errors="replace")
    except HTTPError as exc:
        return exc.code,""

def local_html():
    item=subprocess.run(
        ["curl","--silent","--show-error","--max-time","5",
         "--resolve","bot.arutyun.me:443:127.0.0.1",
         "https://bot.arutyun.me/tg-support/members"],
        capture_output=True,text=True,timeout=8)
    return item.stdout if item.returncode==0 else ""

def public_marker_ready(enabled,checks=24):
    marker='id="manage"'
    for index in range(checks):
        status,body=response("https://bot.arutyun.me/tg-support/members")
        loopback=local_html()
        if status==200 and bool(marker in body)==enabled and bool(marker in loopback)==enabled:
            print("PUBLIC MEMBER UI VERIFIED:", "new actions" if enabled else "previous read-only",
                  "checks",index+1,flush=True)
            return
        if index in (0,4,9,19):
            print("Awaiting Nginx workers, HTTP:",status,
                  "public marker:",marker in body,"local marker:",marker in loopback,flush=True)
        if index+1<checks:time.sleep(0.5)
    raise RuntimeError("Public UI did not converge on expected release marker")

def verify(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Public Nginx is not on expected 8082")
    if bg.release_sha_for_port(8082)!=PUBLIC or bg.release_sha_for_port(8081)!=NEW:
        raise RuntimeError("Admin releases do not match pinned slots")
    for port in (8081,8082):
        if run("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError(f"Admin {port} is unhealthy")
    for port,should in ((8082,False),(8081,True)):
        status,body=response(f"http://127.0.0.1:{port}/tg-support/members")
        if status!=200 or ('id="manage"' in body)!=should:
            raise RuntimeError(f"Local {port} does not expose expected member UI")
    for path,need in (("/login",401),("/tg-support",200),
                      ("/tg-support/api/members",401)):
        got=response("https://bot.arutyun.me"+path)[0]
        if got!=need:raise RuntimeError(f"Public {path}: {got} expected {need}")
    status,body=response("https://bot.arutyun.me/tg-support/members")
    if status!=200 or 'id="manage"' in body:
        raise RuntimeError("Unexpected new UI already public")
    pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if int(pid)<1 or run("systemctl","is-active","dato-telegram-bot.service")!="active":
        raise RuntimeError("Polling bot inactive")
    if run("systemctl","show","dato-telegram-bot.service",
           "-p","WorkingDirectory","--value")!=str(RELEASES/"2d609a3267261e2370e01e02596916d1bdc044c6"):
        raise RuntimeError("Unexpected bot release")
    return pid

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cutover",action="store_true")
    options=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree dirty")
    for sha in (PUBLIC,NEW):
        if run("git","-C",str(ROOT),"rev-parse","--verify",sha)!=sha:
            raise RuntimeError("Pinned Git commit unavailable: "+sha)
        if not (RELEASES/sha/".venv/bin/python").is_file():
            raise RuntimeError("Release Python missing: "+sha)
    sys.path.insert(0,str(RELEASES/PUBLIC/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        bot_pid=verify(bg)
        print("MEMBER ACTIONS CUTOVER PRECHECK OK: public=8082, staged=8081, bot="+bot_pid,flush=True)
        if not options.cutover:
            print("READ-ONLY COMPLETE. Nginx unchanged.")
            return
        print("SWITCHING ONLY NGINX: 8082 -> 8081",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Public Nginx route did not change")
            public_marker_ready(True)
            for path,expected in (("/login",401),
                                  ("/tg-support/api/members",401),
                                  ("/tg-support/api/members/1",401),
                                  ("/tg-support",200)):
                got=response("https://bot.arutyun.me"+path)[0]
                if got!=expected:
                    raise RuntimeError(f"Public {path} unexpected HTTP {got}")
            if run("systemctl","show","dato-telegram-bot.service",
                   "-p","MainPID","--value")!=bot_pid:
                raise RuntimeError("Bot PID changed")
            if bg.release_sha_for_port(8081)!=NEW or not bg.health(8081):
                raise RuntimeError("New public admin failed health check")
            print("MEMBER ACTIONS PUBLIC CUTOVER VERIFIED: 8081, rollback 8082; bot unchanged.")
            print("Manual role-based checks required before using destructive actions.")
        except Exception:
            print("CUTOVER FAILED: restoring previous Nginx 8082",file=sys.stderr)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("CRITICAL: failed to restore Nginx 8082")
            public_marker_ready(False)
            print("PUBLIC PREVIOUS UI RESTORED TO 8082.",file=sys.stderr)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("MEMBER ACTIONS CUTOVER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:300],file=sys.stderr)
        sys.exit(1)
