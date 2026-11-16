#!/usr/bin/env python3
"""Add owner alert tables and stage pinned admin on inactive 8081 only.

The existing 8082 remains public and the bot remains unchanged. Additive
tables default to notifications OFF. Restore old standby if stage fails.
"""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
OLD="800b69ffa6790184467800ad40d0bef0174e0b64"
STANDBY="121b4ef32ec4bff530781df415cc966ced99ab12"
NEW="c1792778c7441dbec7c3efd8d829227493e83c55"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=25).strip()

def http(url):
    try:
        with urlopen(Request(url),timeout=8) as response:
            return response.status,response.read(150000).decode("utf-8",errors="replace")
    except HTTPError as exc:
        return exc.code,""

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working checkout dirty")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned alert commit missing")
    release=RELEASES/NEW
    if not (release/".venv/.install-ok").is_file():
        raise RuntimeError("First run prepare_owner_alerts.py")
    sys.path.insert(0,str(RELEASES/OLD/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8082:
            raise RuntimeError("Public Nginx not on 8082")
        if bg.release_sha_for_port(8082)!=OLD or bg.release_sha_for_port(8081)!=STANDBY:
            raise RuntimeError("Unexpected pinned admin versions")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Unhealthy admin before stage")
        if http("https://bot.arutyun.me/tg-support/api/owner-alerts")[0]!=404:
            raise RuntimeError("Owner alert API unexpectedly already public")
        pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        botdir=run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
        if int(pid)<1 or botdir!=str(RELEASES/BOT):
            raise RuntimeError("Bot unexpectedly changed")
        if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
            raise RuntimeError("Insufficient resource headroom for staging")
        # Reviewed isolated DB-only migration, no historical notification send.
        migration="""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env')
from db.owner_alerts import initialize
asyncio.run(initialize())
print('OWNER ALERT ADDITIVE TABLES READY; DEFAULT NOTIFICATIONS OFF')
"""
        print("INSTALLING ADDITIVE ALERT TABLES (NO SEND)",flush=True)
        subprocess.run([str(release/".venv/bin/python"),"-c",migration],
                       cwd=release,check=True,timeout=40)
        unit=Path("/etc/systemd/system")/bg.UNITS[8081]
        prior=unit.read_text() if unit.exists() else None
        if not prior or run("systemctl","is-active",bg.UNITS[8081])!="active":
            raise RuntimeError("Previous standby service is unavailable")
        print("STAGING OWNER ALERTS ON INACTIVE 8081:",NEW[:12],flush=True)
        replaced=False
        try:
            bg.command("systemctl","stop",bg.UNITS[8081])
            replaced=True
            if bg.health(8081):
                raise RuntimeError("Inactive 8081 still responds after stop")
            unit.write_text(bg.unit_content(release,8081))
            unit.chmod(0o644)
            bg.command("systemctl","daemon-reload")
            bg.command("systemctl","start",bg.UNITS[8081])
            for _ in range(75):
                if bg.health(8081):break
                if bg.command("systemctl","is-active",bg.UNITS[8081],check=False)!="active":
                    raise RuntimeError("Staged admin exited")
                time.sleep(1)
            else:
                raise RuntimeError("Staged admin did not reach health")
            status,body=http("http://127.0.0.1:8081/tg-support/analytics")
            if status!=200 or 'id="owner-alerts"' not in body:
                raise RuntimeError("New owner alert settings not in staged HTML")
            for path in ("/tg-support/api/owner-alerts",
                         "/tg-support/api/analytics",
                         "/tg-support/api/members"):
                if http("http://127.0.0.1:8081"+path)[0]!=401:
                    raise RuntimeError("Telegram signature guard failed on "+path)
            if bg.routing(bg.nginx_path().read_text())!=8082 or not bg.health(8082):
                raise RuntimeError("Public admin changed during staging")
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=pid:
                raise RuntimeError("Bot PID changed")
        except BaseException:
            if replaced:
                print("RESTORING PREVIOUS STANDBY 8081",file=sys.stderr,flush=True)
                bg.command("systemctl","stop",bg.UNITS[8081],check=False)
                unit.write_text(prior)
                bg.command("systemctl","daemon-reload",check=False)
                bg.command("systemctl","start",bg.UNITS[8081],check=False)
                if not bg.health(8081):
                    print("CRITICAL: STANDBY RESTORATION NEEDS REVIEW",file=sys.stderr)
            raise
        print("OWNER ALERT STAGED: 8081",NEW[:12],flush=True)
        print("PUBLIC UNCHANGED: 8082",OLD[:12],flush=True)
        print("BOT UNCHANGED:",pid,flush=True)
        print("No alert timer installed. Owner notifications remain disabled.",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("OWNER ALERT STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
