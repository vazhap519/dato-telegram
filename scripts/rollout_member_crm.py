#!/usr/bin/env python3
"""Pinned, guarded Mini App CRM rollout. No polling bot changes.

Run as root via git show FETCH_HEAD:scripts/rollout_member_crm.py | python - prepare|stage|check|cutover.
Prepare is read-only except isolated release files; stage replaces only inactive 8082.
Check is read-only; cutover changes only Nginx, rollback if validation fails.
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
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
CURRENT="a6eee074d8088ef98819f6777931cd2936148197"
OLD_STANDBY="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
TARGET="bdc9903c962acac7549c9d5a3591ca112f990f68"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
EXPECTED={
 ".github/workflows/member-crm.yml",
 "admin/members_miniapp.py",
 "admin/templates/members_miniapp.html",
 "scripts/cutover_command_center_8081.py",
 "scripts/prepare_command_center.py",
 "scripts/stage_command_center_8081.py",
 "tests/test_member_crm_miniapp.py",
 "tests/test_members_miniapp.py",
}
TESTS=("test_member_crm_miniapp.py","test_members_miniapp.py",
       "test_command_center_miniapp.py","test_miniapp_analytics.py",
       "test_owner_alerts.py","test_support_permissions.py")

def cmd(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def response(url):
    try:
        with urlopen(url,timeout=7) as r:
            return r.status,r.read(200000).decode("utf-8",errors="replace")
    except HTTPError as e:
        return e.code,""

def get_bg():
    sys.path.insert(0,str(RELEASES/CURRENT/"scripts"))
    import deploy_admin_bluegreen as bg
    return bg

def bot_state():
    pid=cmd("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    wd=cmd("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
    if not pid.isdigit() or int(pid)<=0 or wd!=str(RELEASES/BOT):
        raise RuntimeError("Polling bot is not on the expected pinned release")
    if cmd("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alert timer inactive")
    return pid

def check_prereqs(bg):
    if bg.routing(bg.nginx_path().read_text())!=8081:
        raise RuntimeError("Expected public admin port 8081")
    if bg.release_sha_for_port(8081)!=CURRENT or bg.release_sha_for_port(8082)!=OLD_STANDBY:
        raise RuntimeError("Admin release/standby differs from verified state")
    for port in (8081,8082):
        if cmd("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Admin instance unhealthy on "+str(port))
    return bot_state()

def verify_git():
    if cmd("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production git working tree is dirty")
    if cmd("git","-C",str(ROOT),"rev-parse","--verify",TARGET)!=TARGET:
        raise RuntimeError("Pinned CRM commit unavailable; fetch branch first")
    changed=set(cmd("git","-C",str(ROOT),"diff","--name-only",CURRENT,TARGET).splitlines())
    if changed!=EXPECTED:
        raise RuntimeError("Unexpected files in candidate: "+str(sorted(changed.symmetric_difference(EXPECTED))))
    if cmd("git","-C",str(ROOT),"rev-parse",TARGET+":db/subscriber_crm.py")!=cmd("git","-C",str(ROOT),"rev-parse",CURRENT+":db/subscriber_crm.py"):
        raise RuntimeError("Existing CRM timeline implementation modified")
    if cmd("git","-C",str(ROOT),"rev-parse",TARGET+":db/subscriber_profile.py")!=cmd("git","-C",str(ROOT),"rev-parse",CURRENT+":db/subscriber_profile.py"):
        raise RuntimeError("Existing CRM profile implementation modified")

def prepare(bg):
    verify_git()
    pid=check_prereqs(bg)
    if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
        raise RuntimeError("Insufficient RAM/disk headroom")
    release=bg.snapshot(TARGET)
    python=bg.ensure_venv(release)
    for pattern in TESTS:
        subprocess.run([str(python),"-m","unittest","discover","-s",str(release/"tests"),
                        "-p",pattern,"-v"],cwd=release,timeout=120,check=True)
    import re
    scripts=[s for s in re.findall(r"<script>(.*?)</script>",
              (release/"admin/templates/members_miniapp.html").read_text(),re.S) if s.strip()]
    if len(scripts)!=1:raise RuntimeError("Unexpected CRM JS layout")
    if shutil.which("node"):
        with tempfile.NamedTemporaryFile(mode="w",suffix=".js",delete=False) as f:
            f.write(scripts[0]);js_file=f.name
        try:subprocess.run(["node","--check",js_file],check=True,timeout=12)
        finally:Path(js_file).unlink(missing_ok=True)
    # Profile and timeline queries are real and scoped to an existing user.
    # The entire DB verification is read-only; no notes, access or payment writes.
    probe=r"""
import asyncio
from dotenv import load_dotenv
load_dotenv('/opt/dato-telegram/.env',override=True)
from sqlalchemy import text
from db.database import SessionLocal
from db.subscriber_profile import get_subscriber_profile
from db.subscriber_crm import crm_timeline
async def check():
 async with SessionLocal() as s:
  async with s.begin():
   await s.execute(text('SET TRANSACTION READ ONLY'))
   uid=(await s.execute(text('SELECT telegram_id FROM users ORDER BY telegram_id LIMIT 1'))).scalar_one_or_none()
   await s.execute(text('SELECT id,category,status,created_at,updated_at FROM support_tickets WHERE telegram_id=:uid ORDER BY updated_at DESC,id DESC LIMIT 20'),{'uid':uid or -1})
 if uid:
  profile=await get_subscriber_profile(uid)
  timeline=await crm_timeline(uid,limit=60)
  assert 'note' in profile and isinstance(timeline,list)
  print('CRM EXISTING PROFILE/TIMELINE QUERY OK; count:',len(timeline),flush=True)
 else:print('CRM TABLE QUERY OK; no users available to probe',flush=True)
asyncio.run(check())
"""
    subprocess.run([str(python),"-c",probe],cwd=release,check=True,timeout=65)
    print("CRM RELEASE PREPARED:",TARGET,flush=True)
    print("CURRENT PUBLIC 8081 UNCHANGED; BOT PID:",pid,"TIMER UNCHANGED",flush=True)

def stage(bg):
    release=RELEASES/TARGET
    if not (release/".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare first")
    pid=check_prereqs(bg)
    unit=Path("/etc/systemd/system")/bg.UNITS[8082]
    previous=unit.read_text()
    print("STAGING CRM ON INACTIVE 8082",flush=True)
    stopped=False
    try:
        bg.command("systemctl","stop",bg.UNITS[8082])
        stopped=True
        if bg.health(8082):raise RuntimeError("Standby 8082 still reachable after stop")
        unit.write_text(bg.unit_content(release,8082))
        unit.chmod(0o644)
        bg.command("systemctl","daemon-reload")
        bg.command("systemctl","start",bg.UNITS[8082])
        for _ in range(75):
            if bg.health(8082):break
            if bg.command("systemctl","is-active",bg.UNITS[8082],check=False)!="active":
                raise RuntimeError("CRM standby process exited")
            time.sleep(1)
        else:raise RuntimeError("CRM standby startup timeout")
        code,html=response("http://127.0.0.1:8082/tg-support/members")
        if code!=200 or 'id="crm-form"' not in html:
            raise RuntimeError("CRM member interface not present")
        for path in ("/tg-support/api/me","/tg-support/api/members/1/crm",
                     "/tg-support/api/members/1","/tg-support/api/analytics"):
            if response("http://127.0.0.1:8082"+path)[0]!=401:
                raise RuntimeError("Signed API auth check failed: "+path)
        if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081) or bot_state()!=pid:
            raise RuntimeError("Public admin or bot changed")
    except BaseException:
        if stopped:
            print("RESTORING PREVIOUS INACTIVE 8082",flush=True)
            bg.command("systemctl","stop",bg.UNITS[8082],check=False)
            unit.write_text(previous)
            bg.command("systemctl","daemon-reload",check=False)
            bg.command("systemctl","start",bg.UNITS[8082],check=False)
        raise
    print("CRM STAGED: 8082",TARGET[:12],flush=True)
    print("PUBLIC UNCHANGED: 8081",CURRENT[:12],flush=True)
    print("BOT UNCHANGED PID:",pid,"OWNER ALERT TIMER UNCHANGED",flush=True)

def ready(bg):
    if bg.routing(bg.nginx_path().read_text())!=8081:
        raise RuntimeError("Expected public 8081")
    if bg.release_sha_for_port(8081)!=CURRENT or bg.release_sha_for_port(8082)!=TARGET:
        raise RuntimeError("Expected staged CRM on 8082")
    if not bg.health(8081) or not bg.health(8082):
        raise RuntimeError("Admin instance not healthy")
    for path in ("/tg-support/home","/tg-support/members","/tg-support/analytics"):
        if response("https://bot.arutyun.me"+path)[0]!=200:
            raise RuntimeError("Current public page missing "+path)
    for path in ("/tg-support/api/members/1/crm","/tg-support/api/home",
                 "/tg-support/api/owner-alerts"):
        if response("http://127.0.0.1:8082"+path)[0]!=401:
            raise RuntimeError("New candidate API auth failure "+path)
    code,html=response("https://bot.arutyun.me/tg-support/members")
    if code!=200 or 'id="crm-form"' in html:
        raise RuntimeError("Public version already shows new CRM")
    code,html=response("http://127.0.0.1:8082/tg-support/members")
    if code!=200 or 'id="crm-form"' not in html:
        raise RuntimeError("Staged version missing CRM")
    pid=bot_state()
    print("CRM CUTOVER PRECHECK OK: 8081->8082; bot",pid,flush=True)
    return pid

def verify_public(expect_new):
    for n in range(24):
        code,html=response("https://bot.arutyun.me/tg-support/members")
        yes=code==200 and ('id="crm-form"' in html)==expect_new
        if yes:
            print("PUBLIC CRM VERIFIED:",expect_new,"check",n+1,flush=True)
            return
        time.sleep(0.5)
    raise RuntimeError("Nginx public CRM route did not converge")

def rollout():
    p=argparse.ArgumentParser()
    p.add_argument("operation",choices=("prepare","stage","check","cutover"))
    args=p.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Root required")
    bg=get_bg()
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lk:
        fcntl.flock(lk,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.operation=="prepare":prepare(bg);return
        if args.operation=="stage":verify_git();stage(bg);return
        pid=ready(bg)
        if args.operation=="check":
            print("READ ONLY COMPLETE; NO CHANGES",flush=True)
            return
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("Nginx routing mismatch")
            verify_public(True)
            for path in ("/tg-support/api/home","/tg-support/api/members/1/crm",
                         "/tg-support/api/owner-alerts"):
                if response("https://bot.arutyun.me"+path)[0]!=401:
                    raise RuntimeError("Public signed API authorization failure "+path)
            if bot_state()!=pid:raise RuntimeError("Bot process changed")
            print("CRM PUBLIC CUTOVER VERIFIED: 8082; previous 8081 remains rollback",flush=True)
            print("Manually verify notes and tags in Telegram and web admin.",flush=True)
        except Exception:
            print("CRM CUTOVER FAILED; ROLLING BACK NGINX TO 8081",file=sys.stderr,flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("CRITICAL: Nginx rollback failed")
            verify_public(False)
            raise

if __name__=="__main__":
    try:rollout()
    except Exception as e:
        print("CRM ROLLOUT STOPPED:",type(e).__name__,str(e).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
