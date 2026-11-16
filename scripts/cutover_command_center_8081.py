#!/usr/bin/env python3
"""Pinned Nginx-only command centre deployment with rollback.

Default: read-only precheck. Explicit --cutover switches only admin web
from 8082 to staged 8081; no bot or owner alert service changes.
"""
import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
OLD="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
NEW="a6eee074d8088ef98819f6777931cd2936148197"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
MARK='id="tickets"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()
def response(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as resp:
            return resp.status,resp.read(160000).decode("utf-8",errors="replace")
    except HTTPError as exc:return exc.code,""
def local_html():
    r=subprocess.run(["curl","--silent","--show-error","--max-time","6",
                      "--resolve","bot.arutyun.me:443:127.0.0.1",
                      "https://bot.arutyun.me/tg-support/home"],
                     capture_output=True,text=True,timeout=9)
    return r.stdout if r.returncode==0 else ""
def verify_route(enabled):
    for i in range(24):
        status,body=response("https://bot.arutyun.me/tg-support/home")
        local=local_html()
        if enabled and status==200 and MARK in body and MARK in local:
            print("PUBLIC COMMAND CENTRE VERIFIED: new, check",i+1,flush=True)
            return
        if not enabled and status==404 and MARK not in body and MARK not in local:
            print("PREVIOUS PUBLIC ADMIN VERIFIED: home absent, check",i+1,flush=True)
            return
        if i in (0,5,12,19):
            print("Waiting for Nginx workers:",status,"public/local",MARK in body,MARK in local,flush=True)
        if i<23:time.sleep(0.5)
    raise RuntimeError("Public Nginx did not converge on expected release")

def precheck(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected old public 8082")
    if bg.release_sha_for_port(8082)!=OLD or bg.release_sha_for_port(8081)!=NEW:
        raise RuntimeError("Pinned release mismatch")
    for port,expect in ((8082,404),(8081,200)):
        if run("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Admin unhealthy on "+str(port))
        code,html=response(f"http://127.0.0.1:{port}/tg-support/home")
        if code!=expect or (MARK in html)!=(expect==200):
            raise RuntimeError("Unexpected command centre route on port "+str(port))
    for path,expected in (("/login",401),("/tg-support",200),
                          ("/tg-support/members",200),
                          ("/tg-support/analytics",200),
                          ("/tg-support/api/members",401),
                          ("/tg-support/api/analytics",401),
                          ("/tg-support/api/owner-alerts",401)):
        status=response("https://bot.arutyun.me"+path)[0]
        if status!=expected:raise RuntimeError("Public "+path+" returned "+str(status))
    if response("https://bot.arutyun.me/tg-support/home")[0]!=404:
        raise RuntimeError("New hub unexpectedly already public")
    bot=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if not bot.isdigit() or int(bot)<1:
        raise RuntimeError("Bot not running")
    if run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")!=str(RELEASES/BOT):
        raise RuntimeError("Bot release changed")
    if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alert timer inactive")
    return bot

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cutover",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree dirty")
    for sha in (OLD,NEW):
        if run("git","-C",str(ROOT),"rev-parse","--verify",sha)!=sha:
            raise RuntimeError("Pinned Git ref unavailable: "+sha)
    sys.path.insert(0,str(RELEASES/OLD/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pid=precheck(bg)
        print("COMMAND CENTRE CUTOVER PRECHECK OK: public 8082, staged 8081, bot",pid,flush=True)
        if not args.cutover:
            print("READ ONLY COMPLETE. No changes to Nginx or any service.")
            return
        print("SWITCHING ONLY NGINX: 8082 -> 8081",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Nginx routing mismatch")
            verify_route(True)
            for path,expected in (("/login",401),("/tg-support",200),
                                  ("/tg-support/members",200),
                                  ("/tg-support/analytics",200),
                                  ("/tg-support/api/home",401),
                                  ("/tg-support/api/members",401),
                                  ("/tg-support/api/owner-alerts",401)):
                status=response("https://bot.arutyun.me"+path)[0]
                if status!=expected:
                    raise RuntimeError("Public guard check failed "+path+" HTTP "+str(status))
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=pid:
                raise RuntimeError("Bot PID unexpectedly changed")
            if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
                raise RuntimeError("Owner alert timer stopped")
            print("COMMAND CENTRE PUBLIC CUTOVER VERIFIED: 8081, rollback 8082; bot and alerts timer unchanged.",flush=True)
            print("Manual role and navigation checks required inside Telegram.",flush=True)
        except Exception:
            print("Command centre cutover failed, restoring 8082",file=sys.stderr,flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("CRITICAL: failed to restore Nginx 8082")
            verify_route(False)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("COMMAND CENTRE CUTOVER STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
