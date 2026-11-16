#!/usr/bin/env python3
"""Stage reviewed command-centre release on inactive admin 8081.

Public Nginx remains on 8082. No schema changes; no polling bot or
owner-alert timer restart. Recover prior standby after startup failure.
"""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
LIVE="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
OLD_STANDBY="c1792778c7441dbec7c3efd8d829227493e83c55"
NEW="a6eee074d8088ef98819f6777931cd2936148197"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
MARK='id="tickets"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()
def http(url):
    try:
        with urlopen(Request(url),timeout=8) as r:
            return r.status,r.read(150000).decode("utf-8",errors="replace")
    except HTTPError as e:return e.code,""

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production Git tree dirty")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned candidate Git SHA missing")
    release=RELEASES/NEW
    if not (release/".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare_command_center.py first")
    sys.path.insert(0,str(RELEASES/LIVE/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Live Nginx expected on 8082")
        if bg.release_sha_for_port(8082)!=LIVE or bg.release_sha_for_port(8081)!=OLD_STANDBY:
            raise RuntimeError("Unexpected active or standby version")
        if not bg.health(8082) or not bg.health(8081):
            raise RuntimeError("Existing admin instances unhealthy")
        if http("https://bot.arutyun.me/tg-support/home")[0]!=404:
            raise RuntimeError("Home route already public")
        botpid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        botdir=run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
        if int(botpid)<1 or botdir!=str(RELEASES/BOT):
            raise RuntimeError("Unexpected bot release")
        if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
            raise RuntimeError("Owner alert timer inactive")
        if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
            raise RuntimeError("Insufficient free resources for replacement standby")
        unit=Path("/etc/systemd/system")/bg.UNITS[8081]
        previous=unit.read_text() if unit.is_file() else None
        if not previous or run("systemctl","is-active",bg.UNITS[8081])!="active":
            raise RuntimeError("Previous standby not recoverable")
        print("STAGING COMMAND CENTRE TO INACTIVE 8081:",NEW[:12],flush=True)
        stopped=False
        try:
            bg.command("systemctl","stop",bg.UNITS[8081])
            stopped=True
            if bg.health(8081):raise RuntimeError("Inactive port 8081 still responds after stop")
            unit.write_text(bg.unit_content(release,8081))
            unit.chmod(0o644)
            bg.command("systemctl","daemon-reload")
            bg.command("systemctl","start",bg.UNITS[8081])
            for _ in range(75):
                if bg.health(8081):break
                if bg.command("systemctl","is-active",bg.UNITS[8081],check=False)!="active":
                    raise RuntimeError("New admin exited on startup")
                time.sleep(1)
            else:raise RuntimeError("New standby startup timeout")
            status,body=http("http://127.0.0.1:8081/tg-support/home")
            if status!=200 or MARK not in body:
                raise RuntimeError("Command centre not present in staged UI")
            for path in ("/tg-support/api/home","/tg-support/api/members",
                         "/tg-support/api/owner-alerts","/tg-support/api/analytics"):
                if http("http://127.0.0.1:8081"+path)[0]!=401:
                    raise RuntimeError("Signed Telegram auth failed on "+path)
            if bg.routing(bg.nginx_path().read_text())!=8082 or not bg.health(8082):
                raise RuntimeError("Public admin changed during stage")
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=botpid:
                raise RuntimeError("Bot PID changed")
            if run("systemctl","is-active","dato-owner-alerts.timer")!="active":
                raise RuntimeError("Owner alert timer changed")
        except BaseException:
            if stopped:
                print("Restoring previous standby 8081...",flush=True)
                bg.command("systemctl","stop",bg.UNITS[8081],check=False)
                unit.write_text(previous)
                bg.command("systemctl","daemon-reload",check=False)
                bg.command("systemctl","start",bg.UNITS[8081],check=False)
                if not bg.health(8081):
                    print("CRITICAL: previous standby not healthy after restore",file=sys.stderr)
            raise
        print("COMMAND CENTRE STAGED: 8081",NEW[:12],flush=True)
        print("PUBLIC UNCHANGED: 8082",LIVE[:12],flush=True)
        print("BOT UNCHANGED PID:",botpid,flush=True)
        print("OWNER ALERT TIMER UNCHANGED",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("COMMAND CENTRE STAGE STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
