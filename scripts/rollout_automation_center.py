#!/usr/bin/env python3
"""Pinned existing-automation Mini App release: prepare / stage / check / cutover.

All four steps are opt-in. Public is pinned to 8082 and previous standby to
8081. No bot change, no new sender, no DB migration. Prior service restored on
stage failure; Nginx restored on cutover failure.
"""
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request,urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
LIVE="bdc9903c962acac7549c9d5a3591ca112f990f68"
PREVIOUS="a6eee074d8088ef98819f6777931cd2936148197"
NEW="f4e8a10a74bb1894854106b5700394f8853dc566"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
DIFF={
 ".github/workflows/automation-center.yml",
 "admin/members_miniapp.py",
 "admin/templates/automation_center_miniapp.html",
 "admin/templates/command_center_miniapp.html",
 "db/miniapp_automation_center.py",
 "scripts/rollout_member_crm.py",
 "tests/test_automation_center_miniapp.py",
}
TESTS=("test_automation_center_miniapp.py","test_member_crm_miniapp.py",
       "test_members_miniapp.py","test_command_center_miniapp.py",
       "test_miniapp_analytics.py","test_owner_alerts.py",
       "test_support_permissions.py")
MARK='id="expiry-enabled"'

def cmd(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def req(url):
    try:
        with urlopen(Request(url,headers={"Cache-Control":"no-cache"}),timeout=8) as r:
            return r.status,r.read(180000).decode("utf-8",errors="replace")
    except HTTPError as exc:return exc.code,""

def bg_module():
    sys.path.insert(0,str(RELEASES/LIVE/"scripts"))
    import deploy_admin_bluegreen as bg
    return bg

def bot_pid():
    pid=cmd("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    workdir=cmd("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
    if not pid.isdigit() or int(pid)<1 or workdir!=str(RELEASES/BOT):
        raise RuntimeError("Unexpected polling bot process/release")
    if cmd("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alerts timer inactive")
    return pid

def verify_diff():
    if cmd("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production repository has uncommitted changes")
    if cmd("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned candidate not fetched")
    delta=set(cmd("git","-C",str(ROOT),"diff","--name-only",LIVE,NEW).splitlines())
    if delta!=DIFF:
        raise RuntimeError("Unexpected changed files: "+repr(sorted(delta.symmetric_difference(DIFF))))
    for path in ("db/automation.py","db/subscription_automation.py","db/broadcast.py","bot/main.py"):
        old=cmd("git","-C",str(ROOT),"rev-parse",LIVE+":"+path)
        new=cmd("git","-C",str(ROOT),"rev-parse",NEW+":"+path)
        if old!=new:
            raise RuntimeError("Sending engine changed unexpectedly: "+path)

def base_state(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected existing public admin on 8082")
    if bg.release_sha_for_port(8082)!=LIVE or bg.release_sha_for_port(8081)!=PREVIOUS:
        raise RuntimeError("Unexpected public/standby releases")
    for port in (8081,8082):
        if cmd("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Admin unhealthy on port "+str(port))
    if req("https://bot.arutyun.me/tg-support/automations")[0]!=404:
        raise RuntimeError("Automation centre is already publicly available")
    return bot_pid()

def prepare(bg):
    verify_diff()
    pid=base_state(bg)
    if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
        raise RuntimeError("Insufficient RAM or disk")
    release=bg.snapshot(NEW)
    python=bg.ensure_venv(release)
    for pattern in TESTS:
        subprocess.run([str(python),"-m","unittest","discover","-s",str(release/"tests"),
                        "-p",pattern,"-v"],cwd=release,check=True,timeout=120)
    if shutil.which("node"):
        for name in ("automation_center_miniapp.html","command_center_miniapp.html",
                     "members_miniapp.html"):
            scripts=[s for s in re.findall(r"<script>(.*?)</script>",
                    (release/"admin/templates"/name).read_text(),re.S) if s.strip()]
            if len(scripts)!=1:raise RuntimeError("Unexpected JavaScript layout: "+name)
            with tempfile.NamedTemporaryFile(mode="w",suffix=".js",delete=False) as f:
                f.write(scripts[0]);path=f.name
            try:subprocess.run(["node","--check",path],check=True,timeout=15)
            finally:Path(path).unlink(missing_ok=True)
    # Validate dashboard and all existing tables in an actual DB read-only
    # transaction where possible. No send, scheduling or setting mutation.
    probe="""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env',override=True)
from db.miniapp_automation_center import dashboard
async def main():
    d=await asyncio.wait_for(dashboard(),timeout=50)
    assert isinstance(d['rules'],list)
    assert isinstance(d['recent'],list)
    assert isinstance(d['expiry']['enabled'],bool)
    assert len(d['planned'])==3
    print('LIVE READ-ONLY AUTOMATION AUDIT OK:',
          'existing_rules=',len(d['rules']),
          'enabled=',sum(1 for r in d['rules'] if r['enabled']),
          'expiry_enabled=',d['expiry']['enabled'],
          'removal_enabled=',d['expiry']['removal_enabled'],
          'queued=',d['summary']['queued_broadcasts'],flush=True)
asyncio.run(main())
"""
    subprocess.run([str(python),"-c",probe],cwd=release,check=True,timeout=80)
    print("AUTOMATION CENTRE RELEASE PREPARED:",NEW,flush=True)
    print("PUBLIC 8082, BOT PID",pid,"and owner-alert timer UNCHANGED.",flush=True)

def stage(bg):
    verify_diff()
    release=RELEASES/NEW
    if not (release/".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare first")
    pid=base_state(bg)
    if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
        raise RuntimeError("Insufficient RAM/disk for standby")
    unit=Path("/etc/systemd/system")/bg.UNITS[8081]
    previous=unit.read_text()
    stopped=False
    print("STAGING AUTOMATION CENTRE ON INACTIVE 8081",flush=True)
    try:
        bg.command("systemctl","stop",bg.UNITS[8081])
        stopped=True
        if bg.health(8081):raise RuntimeError("Port still in use")
        unit.write_text(bg.unit_content(release,8081))
        unit.chmod(0o644)
        bg.command("systemctl","daemon-reload")
        bg.command("systemctl","start",bg.UNITS[8081])
        for _ in range(75):
            if bg.health(8081):break
            if bg.command("systemctl","is-active",bg.UNITS[8081],check=False)!="active":
                raise RuntimeError("Standby process exited")
            time.sleep(1)
        else:raise RuntimeError("Standby startup timed out")
        code,html=req("http://127.0.0.1:8081/tg-support/automations")
        if code!=200 or MARK not in html:raise RuntimeError("Staged dashboard missing")
        for path in ("/tg-support/api/automations",
                     "/tg-support/api/home",
                     "/tg-support/api/automations/expiry",
                     "/tg-support/api/owner-alerts"):
            if req("http://127.0.0.1:8081"+path)[0]!=401:
                raise RuntimeError("Telegram signature authorization failed for "+path)
        if bg.routing(bg.nginx_path().read_text())!=8082 or not bg.health(8082) or bot_pid()!=pid:
            raise RuntimeError("Public admin, bot or timer changed unexpectedly")
    except BaseException:
        if stopped:
            print("RESTORING PREVIOUS STANDBY 8081",flush=True)
            bg.command("systemctl","stop",bg.UNITS[8081],check=False)
            unit.write_text(previous)
            bg.command("systemctl","daemon-reload",check=False)
            bg.command("systemctl","start",bg.UNITS[8081],check=False)
        raise
    print("AUTOMATION CENTRE STAGED: 8081",NEW[:12],flush=True)
    print("PUBLIC UNCHANGED: 8082",LIVE[:12],flush=True)
    print("BOT UNCHANGED PID:",pid,"OWNER ALERT TIMER UNCHANGED",flush=True)

def precheck(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected Nginx public 8082")
    if bg.release_sha_for_port(8082)!=LIVE or bg.release_sha_for_port(8081)!=NEW:
        raise RuntimeError("Expected old public and new staged versions")
    for port in (8081,8082):
        if cmd("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Unhealthy admin slot "+str(port))
    for port,expected in ((8081,200),(8082,404)):
        status,html=req(f"http://127.0.0.1:{port}/tg-support/automations")
        if status!=expected or ((MARK in html)!=(expected==200)):
            raise RuntimeError("Automation centre route unexpected on "+str(port))
    for path,expected in (("/tg-support/home",200),("/tg-support/members",200),
                          ("/tg-support/analytics",200),("/tg-support/api/home",401),
                          ("/tg-support/api/members",401),("/tg-support/api/owner-alerts",401)):
        if req("https://bot.arutyun.me"+path)[0]!=expected:
            raise RuntimeError("Unexpected public route "+path)
    pid=bot_pid()
    print("AUTOMATION CENTRE CUTOVER PRECHECK OK: 8082 -> 8081; bot",pid,flush=True)
    return pid

def verify_public(new):
    for i in range(24):
        status,html=req("https://bot.arutyun.me/tg-support/automations")
        if status==(200 if new else 404) and ((MARK in html)==new):
            print("PUBLIC AUTOMATION CENTRE VERIFIED:",new,"check",i+1,flush=True)
            return
        time.sleep(.5)
    raise RuntimeError("Public Nginx route did not converge")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("operation",choices=("prepare","stage","check","cutover"))
    a=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    bg=bg_module()
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as file:
        fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if a.operation=="prepare":prepare(bg);return
        if a.operation=="stage":stage(bg);return
        pid=precheck(bg)
        if a.operation=="check":
            print("READ ONLY COMPLETE; NO CHANGES",flush=True);return
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Nginx route mismatch")
            verify_public(True)
            for path in ("/tg-support/api/automations","/tg-support/api/home",
                         "/tg-support/api/owner-alerts","/tg-support/api/members"):
                if req("https://bot.arutyun.me"+path)[0]!=401:
                    raise RuntimeError("Public API signed auth check failed: "+path)
            if bot_pid()!=pid:raise RuntimeError("Bot PID changed")
            print("AUTOMATION CENTRE PUBLIC CUTOVER VERIFIED: 8081, rollback 8082; bot and timer unchanged.",flush=True)
            print("Existing reminder settings are preserved. Review in Telegram before editing.",flush=True)
        except Exception:
            print("AUTOMATION CUTOVER FAILED; RESTORING NGINX TO 8082",file=sys.stderr,flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("CRITICAL: Nginx rollback unsuccessful")
            verify_public(False)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("AUTOMATION ROLLOUT STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
