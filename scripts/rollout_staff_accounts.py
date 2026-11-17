#!/usr/bin/env python3
"""Guarded blue/green rollout for web-admin staff account management.

Usage: python3 rollout_staff_accounts.py prepare|stage|check|cutover --target <full-sha>
Only the web-admin slot and Nginx routing are changed. The Telegram polling bot,
owner-alert timer and lifecycle sender remain untouched.
"""
import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
CURRENT="2d080120e014c8393b31aa1c9eb6b6b57e1898a6"
OLD_STANDBY="f4e8a10a74bb1894854106b5700394f8853dc566"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
EXPECTED={
 "admin/main.py",
 "admin/templates/admin_staff.html",
 "db/admin_access.py",
 "tests/test_support_permissions.py",
 "scripts/rollout_staff_accounts.py",
 # Existing lifecycle rollout hardening carried forward from the deployed base.
 "scripts/rollout_lifecycle.py",
 "tests/test_lifecycle_followups.py",
}
MARK='id="lifecycle-rules"'

def cmd(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def response(url):
    try:
        with urlopen(url,timeout=7) as r:
            return r.status,r.read(180000).decode("utf-8",errors="replace")
    except HTTPError as e:
        return e.code,""

def bg_module():
    sys.path.insert(0,str(RELEASES/CURRENT/"scripts"))
    import deploy_admin_bluegreen
    return deploy_admin_bluegreen

def bot_state():
    pid=cmd("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
    wd=cmd("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
    if not pid.isdigit() or int(pid)<1 or wd!=str(RELEASES/BOT):
        raise RuntimeError("Telegram polling bot PID/release differs from the pinned state")
    if cmd("systemctl","is-active","dato-owner-alerts.timer")!="active":
        raise RuntimeError("Owner alerts timer is not active")
    timer=subprocess.run(["systemctl","is-active","dato-lifecycle-followups.timer"],
                         stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=15)
    if timer.stdout.strip() not in ("inactive","unknown","") or timer.returncode not in (0,3,4):
        raise RuntimeError("Lifecycle sender timer is not in the expected inactive state")
    if timer.stdout.strip()=="active":
        raise RuntimeError("Lifecycle sender timer unexpectedly active")
    return pid

def prereqs(bg):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected current public admin on 8082")
    if bg.release_sha_for_port(8082)!=CURRENT or bg.release_sha_for_port(8081)!=OLD_STANDBY:
        raise RuntimeError("Public or rollback admin release differs from pinned state")
    for port in (8081,8082):
        if cmd("systemctl","is-active",bg.UNITS[port])!="active" or not bg.health(port):
            raise RuntimeError("Admin slot unhealthy on "+str(port))
    return bot_state()

def verify_git(target):
    if cmd("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production Git working tree is dirty")
    if cmd("git","-C",str(ROOT),"rev-parse","--verify",target)!=target:
        raise RuntimeError("Target commit unavailable; fetch the feature branch first")
    changed=set(cmd("git","-C",str(ROOT),"diff","--name-only",CURRENT,target).splitlines())
    if changed!=EXPECTED:
        raise RuntimeError("Unexpected candidate files: "+str(sorted(changed.symmetric_difference(EXPECTED))))
    for name in ("bot/main.py","bot/support_admin.py","db/support_telegram.py",
                 "scripts/deploy_admin_bluegreen.py","db/lifecycle_followups.py"):
        if cmd("git","-C",str(ROOT),"rev-parse",CURRENT+":"+name)!=cmd("git","-C",str(ROOT),"rev-parse",target+":"+name):
            raise RuntimeError("Unrelated bot/support/lifecycle implementation changed: "+name)

def prepare(bg,target):
    verify_git(target)
    pid=prereqs(bg)
    if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
        raise RuntimeError("Insufficient RAM/disk headroom")
    release=bg.snapshot(target)
    python=bg.ensure_venv(release)
    subprocess.run([str(python),"-m","unittest","discover","-s",str(release/"tests"),
                    "-p","test_support_permissions.py","-v"],
                   cwd=release,timeout=120,check=True)
    subprocess.run([str(python),"-m","compileall","-q",str(release/"admin"),
                    str(release/"db"),str(release/"scripts")],
                   cwd=release,timeout=45,check=True)
    probe=r"""
from jinja2 import Environment,FileSystemLoader
env=Environment(loader=FileSystemLoader('admin/templates'))
env.get_template('admin_staff.html')
print('STAFF ADMIN TEMPLATE PARSE OK')
"""
    subprocess.run([str(python),"-c",probe],cwd=release,timeout=20,check=True)
    print("STAFF ACCOUNT RELEASE PREPARED:",target,flush=True)
    print("PUBLIC REMAINS 8082; BOT PID:",pid,"OWNER ALERTS ACTIVE; LIFECYCLE SENDER OFF",flush=True)

def stage(bg,target):
    release=RELEASES/target
    if not (release/".venv/.install-ok").is_file():
        raise RuntimeError("Run prepare first")
    pid=prereqs(bg)
    unit=Path("/etc/systemd/system")/bg.UNITS[8081]
    previous=unit.read_text()
    stopped=False
    print("STAGING STAFF ADMIN ON INACTIVE 8081",flush=True)
    try:
        bg.command("systemctl","stop",bg.UNITS[8081])
        stopped=True
        if bg.health(8081):
            raise RuntimeError("Inactive port 8081 still responds after stop")
        unit.write_text(bg.unit_content(release,8081))
        unit.chmod(0o644)
        bg.command("systemctl","daemon-reload")
        bg.command("systemctl","start",bg.UNITS[8081])
        for _ in range(75):
            if bg.health(8081):break
            if bg.command("systemctl","is-active",bg.UNITS[8081],check=False)!="active":
                raise RuntimeError("Staged admin process exited")
            time.sleep(1)
        else:raise RuntimeError("Staged admin startup timeout")
        status,html=response("http://127.0.0.1:8081/tg-support/automations")
        if status!=200 or MARK not in html:
            raise RuntimeError("Staged admin lost existing lifecycle interface")
        for path in ("/tg-support/api/automations/lifecycle","/tg-support/api/home"):
            if response("http://127.0.0.1:8081"+path)[0]!=401:
                raise RuntimeError("Staged Telegram API auth failure: "+path)
        if bg.routing(bg.nginx_path().read_text())!=8082 or not bg.health(8082) or bot_state()!=pid:
            raise RuntimeError("Public admin, bot or timer state changed during stage")
    except BaseException:
        if stopped:
            print("RESTORING PREVIOUS INACTIVE 8081",flush=True)
            bg.command("systemctl","stop",bg.UNITS[8081],check=False)
            unit.write_text(previous)
            bg.command("systemctl","daemon-reload",check=False)
            bg.command("systemctl","start",bg.UNITS[8081],check=False)
        raise
    print("STAFF ADMIN STAGED ON 8081:",target,flush=True)
    print("PUBLIC UNCHANGED ON 8082; BOT PID UNCHANGED:",pid,flush=True)

def check(bg,target):
    if bg.routing(bg.nginx_path().read_text())!=8082:
        raise RuntimeError("Expected public 8082 before cutover")
    if bg.release_sha_for_port(8082)!=CURRENT or bg.release_sha_for_port(8081)!=target:
        raise RuntimeError("Staged/active release SHA mismatch")
    if not bg.health(8081) or not bg.health(8082):
        raise RuntimeError("Admin slot unhealthy")
    for port in (8081,8082):
        status,html=response(f"http://127.0.0.1:{port}/tg-support/automations")
        if status!=200 or MARK not in html:
            raise RuntimeError("Existing lifecycle UI missing on port "+str(port))
    for path in ("/tg-support/api/automations/lifecycle","/tg-support/api/home"):
        if response("http://127.0.0.1:8081"+path)[0]!=401:
            raise RuntimeError("Staged API authorization failed: "+path)
    pid=bot_state()
    print("STAFF ACCOUNT CUTOVER PRECHECK OK: 8082 -> 8081; bot",pid,flush=True)
    return pid

def verify_public():
    for attempt in range(24):
        status,html=response("https://bot.arutyun.me/tg-support/automations")
        if status==200 and MARK in html:
            print("PUBLIC LIFECYCLE UI VERIFIED; check",attempt+1,flush=True)
            return
        time.sleep(.5)
    raise RuntimeError("Public admin route did not converge after cutover")

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("operation",choices=("prepare","stage","check","cutover"))
    parser.add_argument("--target",required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError("Run as root")
    if len(args.target)!=40 or any(c not in "0123456789abcdef" for c in args.target):
        raise RuntimeError("Target must be a full Git commit SHA")
    bg=bg_module()
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.operation=="prepare":prepare(bg,args.target);return
        if args.operation=="stage":verify_git(args.target);stage(bg,args.target);return
        pid=check(bg,args.target)
        if args.operation=="check":
            print("READ ONLY COMPLETE; NO CHANGES",flush=True)
            return
        bg.cutover()
        try:
            if bg.routing(bg.nginx_path().read_text())!=8081:
                raise RuntimeError("Nginx did not switch to 8081")
            verify_public()
            for path in ("/tg-support/api/automations/lifecycle","/tg-support/api/home"):
                if response("https://bot.arutyun.me"+path)[0]!=401:
                    raise RuntimeError("Public API authorization failed: "+path)
            if bot_state()!=pid:raise RuntimeError("Telegram bot/timer state changed")
            print("STAFF ACCOUNT PUBLIC CUTOVER VERIFIED: 8081; rollback 8082 remains running",flush=True)
            print("No bot restart, no .env changes, no lifecycle sender activation.",flush=True)
        except Exception:
            print("STAFF ACCOUNT CUTOVER FAILED; ROLLING BACK TO 8082",file=sys.stderr,flush=True)
            bg.cutover()
            if bg.routing(bg.nginx_path().read_text())!=8082:
                raise RuntimeError("CRITICAL: Nginx rollback failed")
            raise

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("STAFF ACCOUNT ROLLOUT STOPPED:",type(exc).__name__,
              str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
