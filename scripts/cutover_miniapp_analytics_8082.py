#!/usr/bin/env python3
"""Guarded Nginx-only analytics cutover, default read-only, --cutover explicit.

Only use after preparing and staging the exact pinned SHA on 8082.
Previous admin stays on 8081 for rollback; polling bot untouched.
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
OLD="121b4ef32ec4bff530781df415cc966ced99ab12"
NEW="800b69ffa6790184467800ad40d0bef0174e0b64"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
MARKER='id="paid-subscription-users"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()

def get(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as r:
            return r.status,r.read(150000).decode("utf-8",errors="replace")
    except HTTPError as exc:
        return exc.code,""

def local_route():
    r=subprocess.run(["curl","--silent","--show-error","--max-time","6",
                      "--resolve","bot.arutyun.me:443:127.0.0.1",
                      "https://bot.arutyun.me/tg-support/analytics"],
                     capture_output=True,text=True,timeout=9)
    return r.stdout if r.returncode==0 else ""

def wait_for_analytics(enabled,attempts=24):
    for i in range(attempts):
        code,html=get("https://bot.arutyun.me/tg-support/analytics")
        local=local_route()
        if enabled:
            good=(code==200 and MARKER in html and MARKER in local)
        else:
            good=(code==404 and MARKER not in html and MARKER not in local)
        if good:
            print("ANALYTICS PUBLIC ROUTE VERIFIED:", "new" if enabled else "previous",
                  "check",i+1,flush=True)
            return
        if i in (0,4,9,19):
            print("Waiting for Nginx: code",code,"public/local markers",MARKER in html,MARKER in local,flush=True)
        if i+1<attempts:time.sleep(0.5)
    raise RuntimeError("Public analytics route did not converge after reload")

def verify(bg):
    if bg.routing(bg.nginx_path().read_text())!=8081:
        raise RuntimeError("Expected public Nginx 8081")
    if bg.release_sha_for_port(8081)!=OLD or bg.release_sha_for_port(8082)!=NEW:
        raise RuntimeError("Unexpected pinned releases")
    for port in (8081,8082):
        if run("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Unhealthy admin slot "+str(port))
    for port,expected in ((8081,404),(8082,200)):
        status,body=get(f"http://127.0.0.1:{port}/tg-support/analytics")
        if status!=expected or (MARKER in body)!=(expected==200):
            raise RuntimeError("Unexpected analytics UI on port "+str(port))
    for url,expected in (("/login",401),("/tg-support",200),
                          ("/tg-support/members",200),
                          ("/tg-support/api/members",401)):
        got=get("https://bot.arutyun.me"+url)[0]
        if got!=expected:raise RuntimeError("Existing public endpoint "+url+" HTTP "+str(got))
    if get("https://bot.arutyun.me/tg-support/analytics")[0]!=404:
        raise RuntimeError("Analytics unexpectedly already public")
    pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if int(pid)<=0 or run("systemctl","is-active","dato-telegram-bot.service")!="active":
        raise RuntimeError("Telegram bot inactive")
    if run("systemctl","show","dato-telegram-bot.service",
           "-p","WorkingDirectory","--value")!=str(RELEASES/BOT):
        raise RuntimeError("Unexpected bot release")
    return pid

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cutover",action="store_true",help="Switch ONLY Nginx 8081 -> 8082")
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Git working tree dirty")
    for sha in (OLD,NEW):
        if run("git","-C",str(ROOT),"rev-parse","--verify",sha)!=sha:
            raise RuntimeError("Pinned commit unavailable: "+sha)
    sys.path.insert(0,str(RELEASES/OLD/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        bot_pid=verify(bg)
        print("ANALYTICS CUTOVER PRECHECK OK: public 8081, new 8082, bot",bot_pid,flush=True)
        if not args.cutover:
            print("READ-ONLY COMPLETE. Nginx unchanged.")
            return
        print("SWITCHING ONLY NGINX: 8081 -> 8082",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("Unexpected new Nginx route")
            wait_for_analytics(True)
            for path,expected in (("/login",401),("/tg-support",200),
                                  ("/tg-support/members",200),
                                  ("/tg-support/api/analytics",401),
                                  ("/tg-support/api/analytics?period=all",401),
                                  ("/tg-support/api/members",401)):
                got=get("https://bot.arutyun.me"+path)[0]
                if got!=expected:
                    raise RuntimeError("Public guard failed "+path+" HTTP "+str(got))
            if run("systemctl","show","dato-telegram-bot.service",
                   "-p","MainPID","--value")!=bot_pid:
                raise RuntimeError("Bot PID changed")
            if bg.release_sha_for_port(8082)!=NEW or not bg.health(8082):
                raise RuntimeError("New public admin unhealthy")
            print("ANALYTICS PUBLIC CUTOVER VERIFIED: 8082, rollback 8081; bot unchanged.",flush=True)
            print("Signed Telegram owner/admin role and displayed totals still need manual verification.",flush=True)
        except Exception:
            print("Analytics validation failed; restoring public 8081",file=sys.stderr)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("CRITICAL: Nginx rollback failed")
            wait_for_analytics(False)
            print("PREVIOUS PUBLIC ADMIN RESTORED AND VERIFIED.",file=sys.stderr)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("ANALYTICS CUTOVER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
