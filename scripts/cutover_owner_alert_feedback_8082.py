#!/usr/bin/env python3
"""Cutover pinned UI-only feedback patch; default is read-only precheck.

Switch Nginx from 8081 to staged 8082. Previous 8081 stays alive for rollback.
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
OLD="c1792778c7441dbec7c3efd8d829227493e83c55"
NEW="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
MARK='id="alert-feedback"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()
def request(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as r:
            return r.status,r.read(150000).decode("utf-8",errors="replace")
    except HTTPError as ex:
        return ex.code,""
def local_html():
    p=subprocess.run(["curl","-sS","--max-time","6",
                      "--resolve","bot.arutyun.me:443:127.0.0.1",
                      "https://bot.arutyun.me/tg-support/analytics"],
                     text=True,capture_output=True,timeout=9)
    return p.stdout if p.returncode==0 else ""
def wait(expected):
    for i in range(24):
        code,body=request("https://bot.arutyun.me/tg-support/analytics")
        local=local_html()
        if code==200 and (MARK in body)==expected and (MARK in local)==expected:
            print("PUBLIC INLINE FEEDBACK VERIFIED:",expected,"check",i+1,flush=True)
            return
        if i in (0,5,12,19):
            print("Waiting Nginx workers; code",code,"public/local",MARK in body,MARK in local,flush=True)
        if i<23:time.sleep(0.5)
    raise RuntimeError("Public Nginx did not converge to expected UI")

def verify(bg):
    if bg.routing(bg.nginx_path().read_text())!=8081:
        raise RuntimeError("Expected live 8081")
    if bg.release_sha_for_port(8081)!=OLD or bg.release_sha_for_port(8082)!=NEW:
        raise RuntimeError("Expected pinned old/new releases")
    for port,expected in ((8081,False),(8082,True)):
        if run("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Admin slot unhealthy: "+str(port))
        code,body=request(f"http://127.0.0.1:{port}/tg-support/analytics")
        if code!=200 or (MARK in body)!=expected:
            raise RuntimeError("Wrong UI on port "+str(port))
    for url,expected in (("/login",401),("/tg-support",200),
                          ("/tg-support/api/owner-alerts",401),
                          ("/tg-support/api/analytics",401),
                          ("/tg-support/api/members",401)):
        got=request("https://bot.arutyun.me"+url)[0]
        if got!=expected:raise RuntimeError(url+" returned "+str(got))
    bot=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if not bot.isdigit() or int(bot)<1:
        raise RuntimeError("Bot inactive")
    if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alerts timer inactive")
    return bot

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--cutover",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    sys.path.insert(0,str(RELEASES/OLD/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        pid=verify(bg)
        print("INLINE FEEDBACK CUTOVER PRECHECK OK: 8081->8082; bot",pid,flush=True)
        if not args.cutover:
            print("READ ONLY COMPLETE; NO CHANGES")
            return
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("Wrong new Nginx route")
            wait(True)
            for url,expected in (("/login",401),("/tg-support",200),
                                 ("/tg-support/api/owner-alerts",401),
                                 ("/tg-support/api/analytics",401)):
                if request("https://bot.arutyun.me"+url)[0]!=expected:
                    raise RuntimeError("Public security check failed: "+url)
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=pid:
                raise RuntimeError("Bot process changed")
            if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
                raise RuntimeError("Alert timer stopped unexpectedly")
            print("INLINE FEEDBACK CUTOVER VERIFIED: 8082, rollback 8081; bot and alerts timer unchanged.",flush=True)
        except Exception:
            print("ROLLING BACK NGINX TO 8081",file=sys.stderr,flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("CRITICAL: rollback unsuccessful")
            wait(False)
            raise

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("INLINE FEEDBACK CUTOVER STOPPED:",type(e).__name__,str(e).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
