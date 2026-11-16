#!/usr/bin/env python3
"""Read-only precheck by default. Explicit --cutover switches only public Nginx.

Old analytics release remains running on 8082 for rollback. The alert
timer is not installed or started by this script.
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
OLD="800b69ffa6790184467800ad40d0bef0174e0b64"
NEW="c1792778c7441dbec7c3efd8d829227493e83c55"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
MARKER='id="owner-alerts"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()
def fetch(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as r:
            return r.status,r.read(150000).decode("utf-8",errors="replace")
    except HTTPError as exc:
        return exc.code,""
def loopback():
    r=subprocess.run(["curl","--silent","--show-error","--max-time","6",
                      "--resolve","bot.arutyun.me:443:127.0.0.1",
                      "https://bot.arutyun.me/tg-support/analytics"],
                     capture_output=True,text=True,timeout=9)
    return r.stdout if r.returncode==0 else ""

def wait_route(should_have,checks=24):
    for i in range(checks):
        status,body=fetch("https://bot.arutyun.me/tg-support/analytics")
        local=loopback()
        if status==200 and (MARKER in body)==should_have and (MARKER in local)==should_have:
            print("OWNER ALERT PUBLIC MARKER VERIFIED:",should_have,"check",i+1,flush=True)
            return
        if i in (0,5,12,19):
            print("Waiting for Nginx workers:",status,
                  "public/local marker",MARKER in body,MARKER in local,flush=True)
        if i+1<checks:time.sleep(0.5)
    raise RuntimeError("Nginx did not converge to expected owner alerts UI")

def verify(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected live 8082")
    if bg.release_sha_for_port(8082)!=OLD or bg.release_sha_for_port(8081)!=NEW:
        raise RuntimeError("Expected pinned releases")
    for port in (8081,8082):
        if run("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Unhealthy admin "+str(port))
        code,body=fetch(f"http://127.0.0.1:{port}/tg-support/analytics")
        if code!=200 or (MARKER in body)!=(port==8081):
            raise RuntimeError("Unexpected alert settings UI "+str(port))
    for path,expected in (("/login",401),("/tg-support",200),
                          ("/tg-support/api/members",401),
                          ("/tg-support/api/analytics",401)):
        status=fetch("https://bot.arutyun.me"+path)[0]
        if status!=expected:
            raise RuntimeError("Public route "+path+" HTTP "+str(status))
    if fetch("https://bot.arutyun.me/tg-support/api/owner-alerts")[0]!=404:
        raise RuntimeError("Owner alerts endpoint unexpectedly public already")
    pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    if int(pid)<1 or run("systemctl","is-active","dato-telegram-bot.service")!="active":
        raise RuntimeError("Bot inactive")
    if run("systemctl","show","dato-telegram-bot.service",
           "-p","WorkingDirectory","--value")!=str(RELEASES/BOT):
        raise RuntimeError("Bot release unexpectedly changed")
    return pid

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--cutover",action="store_true")
    opts=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working checkout modified")
    for sha in (OLD,NEW):
        if run("git","-C",str(ROOT),"rev-parse","--verify",sha)!=sha:
            raise RuntimeError("Pinned SHA missing "+sha)
    sys.path.insert(0,str(RELEASES/OLD/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        botpid=verify(bg)
        print("OWNER ALERT CUTOVER PRECHECK OK: public 8082, new 8081, bot",botpid,flush=True)
        if not opts.cutover:
            print("READ ONLY COMPLETE. No route or timer changes.")
            return
        print("SWITCHING ONLY NGINX 8082 -> 8081",flush=True)
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Nginx route incorrect")
            wait_route(True)
            for path,needed in (("/login",401),("/tg-support",200),
                                ("/tg-support/members",200),
                                ("/tg-support/api/owner-alerts",401),
                                ("/tg-support/api/analytics",401),
                                ("/tg-support/api/members",401)):
                got=fetch("https://bot.arutyun.me"+path)[0]
                if got!=needed:
                    raise RuntimeError("Public "+path+" returned HTTP "+str(got))
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=botpid:
                raise RuntimeError("Polling bot changed")
            print("OWNER ALERT PUBLIC CUTOVER VERIFIED: 8081, rollback 8082; bot unchanged.",flush=True)
            print("Timer NOT INSTALLED; all notifications opt-in/off by default.",flush=True)
        except Exception:
            print("CUTOVER FAILED: rolling Nginx back to 8082",file=sys.stderr)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("CRITICAL NGINX ROLLBACK FAILURE")
            wait_route(False)
            print("PREVIOUS PUBLIC 8082 RESTORED.",file=sys.stderr)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("OWNER ALERT CUTOVER STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
