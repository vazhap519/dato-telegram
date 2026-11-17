#!/usr/bin/env python3
"""Pinned lifecycle Mini App rollout. prepare -> stage -> check -> cutover.

prepare: isolated snapshot, tests, no DB writes.
stage: replace standby 8082; additive empty lifecycle tables created on web
       startup, no messages queued and no sender timer installed.
cutover: only Nginx switches. Bot and owner alerts must remain untouched.
"""
import argparse
import fcntl
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
LIVE="f4e8a10a74bb1894854106b5700394f8853dc566"
PREVIOUS="bdc9903c962acac7549c9d5a3591ca112f990f68"
TARGET="2d080120e014c8393b31aa1c9eb6b6b57e1898a6"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
ALLOWED={
 ".github/workflows/lifecycle-followups.yml","admin/main.py",
 "admin/members_miniapp.py","admin/templates/automation_center_miniapp.html",
 "db/lifecycle_followups.py","db/miniapp_automation_center.py",
 "scripts/lifecycle_scheduler.py","scripts/rollout_automation_center.py",
 "tests/test_automation_center_miniapp.py","tests/test_lifecycle_followups.py",
}
TESTS=("test_lifecycle_followups.py","test_automation_center_miniapp.py",
       "test_member_crm_miniapp.py","test_members_miniapp.py",
       "test_command_center_miniapp.py","test_miniapp_analytics.py",
       "test_owner_alerts.py","test_support_permissions.py")
MARK='id="lifecycle-rules"'

def cmd(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def req(url):
    try:
        with urlopen(url,timeout=7) as r:
            return r.status,r.read(180000).decode("utf-8",errors="replace")
    except HTTPError as e:return e.code,""

def bg():
    sys.path.insert(0,str(RELEASES/LIVE/"scripts"))
    import deploy_admin_bluegreen
    return deploy_admin_bluegreen

def bot_state():
    pid=cmd("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    wd=cmd("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
    if not pid.isdigit() or int(pid)<1 or wd!=str(RELEASES/BOT):
        raise RuntimeError("Unexpected polling bot PID or pinned release")
    if cmd("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alerts timer inactive")
    # systemctl returns 4 when the opt-in timer unit does not yet exist.
    # That is the expected safe state, not a release failure.
    timer=subprocess.run(
        ["systemctl","is-active","dato-lifecycle-followups.timer"],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
    if timer.stdout.strip() not in ("inactive","unknown","") or timer.returncode not in (0,3,4):
        raise RuntimeError("Lifecycle scheduler state is unsafe: "+timer.stdout.strip())
    if timer.stdout.strip()=="active":
        raise RuntimeError("Unexpected lifecycle scheduler active")
    return pid

def verify_diff():
    if cmd("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working tree dirty")
    if cmd("git","-C",str(ROOT),"rev-parse","--verify",TARGET)!=TARGET:
        raise RuntimeError("Candidate commit unavailable; fetch feature branch")
    diff=set(cmd("git","-C",str(ROOT),"diff","--name-only",LIVE,TARGET).splitlines())
    if diff!=ALLOWED:
        raise RuntimeError("Unexpected release changes: "+str(sorted(diff.symmetric_difference(ALLOWED))))
    for name in ("bot/main.py","db/broadcast.py","db/automation.py",
                 "db/subscriptions.py","db/tribute_auto.py"):
        if cmd("git","-C",str(ROOT),"rev-parse",LIVE+":"+name)!=cmd("git","-C",str(ROOT),"rev-parse",TARGET+":"+name):
            raise RuntimeError("Unsafe alteration of running bot/queue: "+name)

def base_state(deploy):
    if deploy.routing(deploy.nginx_path().read_text())!=8081:
        raise RuntimeError("Public not on expected 8081")
    if deploy.release_sha_for_port(8081)!=LIVE or deploy.release_sha_for_port(8082)!=PREVIOUS:
        raise RuntimeError("Public or rollback release differs from pinned version")
    for port in (8081,8082):
        if cmd("systemctl","is-active",deploy.UNITS[port])!="active" or not deploy.health(port):
            raise RuntimeError("Admin port unhealthy "+str(port))
    if req("https://bot.arutyun.me/tg-support/api/automations/lifecycle")[0]!=404:
        raise RuntimeError("Public lifecycle endpoint already present")
    return bot_state()

def prepare(deploy):
    verify_diff()
    pid=base_state(deploy)
    if deploy.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
        raise RuntimeError("Insufficient RAM/disk")
    release=deploy.snapshot(TARGET)
    python=deploy.ensure_venv(release)
    for name in TESTS:
        subprocess.run([str(python),"-m","unittest","discover","-s",str(release/"tests"),
                        "-p",name,"-v"],cwd=release,timeout=120,check=True)
    subprocess.run([str(python),"-m","compileall","-q",str(release/"db"),
                    str(release/"admin"),str(release/"scripts")],
                   cwd=release,timeout=35,check=True)
    print("LIFECYCLE RELEASE PREPARED:",TARGET,flush=True)
    print("PUBLIC UNCHANGED: 8081; BOT PID:",pid,"TIMER UNCHANGED",flush=True)
    print("NO LIFECYCLE TABLES OR MESSAGES CREATED IN PREPARE",flush=True)

def stage(deploy):
    verify_diff()
    if not (RELEASES/TARGET/".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare first")
    pid=base_state(deploy)
    if deploy.memory_mib()<180:raise RuntimeError("Insufficient RAM")
    unit=Path("/etc/systemd/system")/deploy.UNITS[8082]
    prev=unit.read_text()
    changed=False
    print("STAGING LIFECYCLE WEB UI TO INACTIVE 8082",flush=True)
    try:
        deploy.command("systemctl","stop",deploy.UNITS[8082])
        changed=True
        if deploy.health(8082):raise RuntimeError("Standby port remained occupied")
        unit.write_text(deploy.unit_content(RELEASES/TARGET,8082))
        unit.chmod(0o644)
        deploy.command("systemctl","daemon-reload")
        deploy.command("systemctl","start",deploy.UNITS[8082])
        for _ in range(75):
            if deploy.health(8082):break
            if deploy.command("systemctl","is-active",deploy.UNITS[8082],check=False)!="active":
                raise RuntimeError("Staged admin exited")
            time.sleep(1)
        else:raise RuntimeError("Staged admin timed out")
        status,html=req("http://127.0.0.1:8082/tg-support/automations")
        if status!=200 or MARK not in html:
            raise RuntimeError("Lifecycle interface not present")
        for path in ("/tg-support/api/automations/lifecycle",
                     "/tg-support/api/automations","/tg-support/api/home"):
            if req("http://127.0.0.1:8082"+path)[0]!=401:
                raise RuntimeError("Unsigned Telegram API accepted "+path)
        # Read-only smoke test of new additive tables and dry-run logic.
        script=r"""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env',override=True)
from db.lifecycle_followups import rules,dry_run
async def main():
 r=await rules()
 d=await dry_run()
 assert len(r)==3 and all(x['enabled'] is False for x in r)
 assert d['inactive']['eligible_now']==0
 print('STAGED LIFECYCLE READ-ONLY PROBE OK: 3 rules, all disabled; no sends',flush=True)
asyncio.run(main())
"""
        subprocess.run([str(RELEASES/TARGET/".venv/bin/python"),"-c",script],
                       cwd=RELEASES/TARGET,timeout=60,check=True)
        if deploy.routing(deploy.nginx_path().read_text())!=8081 or not deploy.health(8081) or bot_state()!=pid:
            raise RuntimeError("Public, bot or timers changed")
    except BaseException:
        if changed:
            print("RESTORING INACTIVE ADMIN 8082",flush=True)
            deploy.command("systemctl","stop",deploy.UNITS[8082],check=False)
            unit.write_text(prev)
            deploy.command("systemctl","daemon-reload",check=False)
            deploy.command("systemctl","start",deploy.UNITS[8082],check=False)
        raise
    print("LIFECYCLE STAGED: 8082",TARGET[:12],flush=True)
    print("PUBLIC UNCHANGED: 8081",LIVE[:12],flush=True)
    print("BOT PID UNCHANGED:",pid,"NO LIFECYCLE SCHEDULER",flush=True)

def check(deploy):
    if deploy.routing(deploy.nginx_path().read_text())!=8081:
        raise RuntimeError("Expected public 8081")
    if deploy.release_sha_for_port(8081)!=LIVE or deploy.release_sha_for_port(8082)!=TARGET:
        raise RuntimeError("Staged release mismatch")
    if not deploy.health(8081) or not deploy.health(8082):
        raise RuntimeError("Admin unhealthy")
    for port,expect in ((8081,False),(8082,True)):
        status,html=req(f"http://127.0.0.1:{port}/tg-support/automations")
        if status!=200 or ((MARK in html)!=expect):
            raise RuntimeError("Lifecycle UI wrong on port "+str(port))
    for path in ("/tg-support/api/automations/lifecycle",
                 "/tg-support/api/automations","/tg-support/api/members"):
        if req("http://127.0.0.1:8082"+path)[0]!=401:
            raise RuntimeError("Staged Telegram API auth failed")
    pid=bot_state()
    print("LIFECYCLE CUTOVER PRECHECK OK: 8081 -> 8082; bot",pid,flush=True)
    return pid

def verify_public(yes):
    for attempt in range(24):
        status,html=req("https://bot.arutyun.me/tg-support/automations")
        if status==200 and ((MARK in html)==yes):
            print("PUBLIC LIFECYCLE VERIFIED:",yes,"check",attempt+1,flush=True)
            return
        time.sleep(.5)
    raise RuntimeError("Public route did not converge")

def verify_public_lifecycle_api():
    # Nginx reload is graceful: a request immediately after cutover can still
    # be served by an old worker/upstream. Retry, but accept ONLY the app's
    # expected unauthenticated response; 404/5xx never count as success.
    url="https://bot.arutyun.me/tg-support/api/automations/lifecycle"
    last=None
    for attempt in range(24):
        last=req(url)[0]
        if last==401:
            print("PUBLIC LIFECYCLE API AUTH VERIFIED: HTTP 401; check",attempt+1,flush=True)
            return
        time.sleep(.5)
    raise RuntimeError("Unsigned public lifecycle API not rejected; last HTTP "+str(last))

def main():
    p=argparse.ArgumentParser()
    p.add_argument("operation",choices=("prepare","stage","check","cutover"))
    args=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    deploy=bg()
    deploy.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with deploy.LOCK.open("a+") as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.operation=="prepare":prepare(deploy);return
        if args.operation=="stage":stage(deploy);return
        pid=check(deploy)
        if args.operation=="check":
            print("READ ONLY COMPLETE; NO CHANGES",flush=True);return
        deploy.cutover()
        try:
            if deploy.routing(deploy.nginx_path().read_text())!=8082:
                raise RuntimeError("Nginx did not switch")
            verify_public(True)
            verify_public_lifecycle_api()
            if bot_state()!=pid:raise RuntimeError("Bot changed")
            print("LIFECYCLE PUBLIC CUTOVER VERIFIED: 8082; rollback 8081; sender timer NOT installed.",flush=True)
        except Exception:
            print("LIFECYCLE CUTOVER FAILED; ROLLING BACK TO 8081",file=sys.stderr,flush=True)
            deploy.cutover()
            verify_public(False)
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("LIFECYCLE ROLLOUT STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
