#!/usr/bin/env python3
"""Stage analytics release on inactive 8082, leaving public 8081 untouched."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import shutil
from urllib.error import HTTPError
from urllib.request import Request,urlopen

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
PUBLIC="121b4ef32ec4bff530781df415cc966ced99ab12"
STANDBY="f7ea20273c15e170bb58f8eaa97341e84974ec77"
NEW="800b69ffa6790184467800ad40d0bef0174e0b64"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"

def run(*args):
    return subprocess.check_output(args,text=True,timeout=20).strip()
def response(url):
    try:
        with urlopen(Request(url),timeout=8) as r:return r.status
    except HTTPError as e:return e.code

def main():
    if os.geteuid()!=0:raise RuntimeError("Root required")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Working Git tree dirty")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned commit missing")
    if not (RELEASES/NEW/".venv/.install-ok").is_file():
        raise RuntimeError("First run prepare_miniapp_analytics.py")
    sys.path.insert(0,str(RELEASES/PUBLIC/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8081:
            raise RuntimeError("Unexpected Nginx active port")
        if bg.release_sha_for_port(8081)!=PUBLIC or bg.release_sha_for_port(8082)!=STANDBY:
            raise RuntimeError("Unexpected live and standby revisions")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Admin health failed before staging")
        if response("https://bot.arutyun.me/tg-support/analytics")!=404:
            raise RuntimeError("Analytics URL is already public; investigate first")
        pid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        botdir=run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")
        if int(pid)<1 or botdir!=str(RELEASES/BOT):
            raise RuntimeError("Bot changed or inactive")
        # The generic bg.stage() only permits origin/main and blocks ALL
        # new db/ files. This dedicated path permits the exact reviewed
        # analytics commit and its sole new, read-only DB module. It does
        # not change repository refs or weaken global Blue/Green policy.
        base=PUBLIC
        changed=set(run("git","-C",str(ROOT),"diff","--name-only",base,NEW).splitlines())
        allowed={".github/workflows/miniapp-analytics.yml",
                 "admin/members_miniapp.py",
                 "admin/templates/analytics_miniapp.html",
                 "admin/templates/members_miniapp.html",
                 "admin/templates/support_miniapp.html",
                 "db/miniapp_analytics.py",
                 "scripts/cutover_member_actions_8081.py",
                 "scripts/prepare_member_actions_candidate.py",
                 "scripts/stage_member_actions_8081.py",
                 "tests/test_miniapp_analytics.py"}
        if changed!=allowed:
            raise RuntimeError("Candidate file changes differ from reviewed set: "+str(sorted(changed.symmetric_difference(allowed))))
        if bg.memory_mib()<220 or shutil.disk_usage(ROOT).free<1024**3:
            raise RuntimeError("Insufficient memory or disk for standby stage")
        new_release=RELEASES/NEW
        if not (new_release/"admin/main.py").is_file() or not (new_release/".venv/.install-ok").is_file():
            raise RuntimeError("Prepared pinned snapshot incomplete")
        print("STAGING PINNED ANALYTICS RELEASE ON INACTIVE 8082",flush=True)
        unit=Path("/etc/systemd/system")/bg.UNITS[8082]
        previous_content=unit.read_text() if unit.is_file() else None
        previous_running=run("systemctl","is-active",bg.UNITS[8082])=="active"
        if not previous_running or previous_content is None:
            raise RuntimeError("Previous standby unit not available for rollback")
        # The same reviewed stop/start and restore algorithm as bg.stage(),
        # narrowed to the known inactive 8082 and one exact candidate.
        bg.command("systemctl","stop",bg.UNITS[8082])
        try:
            if bg.health(8082):
                raise RuntimeError("Inactive port 8082 still responding after stop")
            unit.write_text(bg.unit_content(new_release,8082))
            unit.chmod(0o644)
            bg.command("systemctl","daemon-reload")
            bg.command("systemctl","start",bg.UNITS[8082])
            for attempt in range(75):
                if bg.health(8082):
                    break
                if bg.command("systemctl","is-active",bg.UNITS[8082],check=False)!="active":
                    raise RuntimeError("New standby exited during startup")
                time.sleep(1)
            else:
                raise RuntimeError("New analytics standby did not start")
        except Exception:
            bg.command("systemctl","stop",bg.UNITS[8082],check=False)
            unit.write_text(previous_content)
            bg.command("systemctl","daemon-reload",check=False)
            bg.command("systemctl","start",bg.UNITS[8082],check=False)
            if not bg.health(8082):
                raise RuntimeError("CRITICAL: rollback attempted but previous standby is unhealthy")
            print("Standby 8082 restored to prior release; public 8081 unchanged.",file=sys.stderr)
            raise
        if bg.release_sha_for_port(8082)!=NEW or not bg.health(8082):
            raise RuntimeError("New analytics standby failed health check")
        if response("http://127.0.0.1:8082/tg-support/analytics")!=200:
            raise RuntimeError("Analytics HTML endpoint unavailable")
        for path in ("/tg-support/api/analytics",
                     "/tg-support/api/analytics?period=all",
                     "/tg-support/api/members",
                     "/tg-support/api/members/1"):
            if response("http://127.0.0.1:8082"+path)!=401:
                raise RuntimeError("Expected signed Telegram guard on "+path)
        if response("http://127.0.0.1:8082/login")!=200:
            raise RuntimeError("Login route unexpectedly changed")
        if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081):
            raise RuntimeError("Public admin changed while staging")
        if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=pid:
            raise RuntimeError("Bot PID changed while staging")
        print("ANALYTICS STAGED: 8082 =",NEW[:12],flush=True)
        print("PUBLIC UNCHANGED: 8081 =",PUBLIC[:12],flush=True)
        print("BOT UNCHANGED PID:",pid,flush=True)

if __name__=="__main__":
    try:main()
    except Exception as exc:
        print("ANALYTICS STAGE STOPPED:",type(exc).__name__,str(exc).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
