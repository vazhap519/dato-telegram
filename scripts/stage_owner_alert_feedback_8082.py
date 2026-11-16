#!/usr/bin/env python3
"""Prepare and stage a UI-only owner-alert feedback fix on inactive 8082.

Does not change the public 8081, polling bot, DB or owner-alert timer.
The exact reviewed commit is pinned; no global stage guard is weakened.
"""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import re
import tempfile
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT=Path("/opt/dato-telegram")
RELEASES=Path("/opt/dato-admin-releases")
OLD="800b69ffa6790184467800ad40d0bef0174e0b64"
LIVE="c1792778c7441dbec7c3efd8d829227493e83c55"
NEW="da7e07eaf7e79cf7228842efa4c77df0bec0ac45"
BOT="2d609a3267261e2370e01e02596916d1bdc044c6"
UI="admin/templates/analytics_miniapp.html"
MARK='id="alert-feedback"'

def run(*args):
    return subprocess.check_output(args,text=True,timeout=30).strip()

def get(url):
    try:
        with urlopen(url,timeout=6) as res:
            return res.status,res.read(160000).decode("utf-8",errors="replace")
    except HTTPError as e:return e.code,""

def main():
    if os.geteuid()!=0:raise RuntimeError("Requires root")
    if run("git","-C",str(ROOT),"status","--porcelain"):
        raise RuntimeError("Production working tree must be clean")
    if run("git","-C",str(ROOT),"rev-parse","--verify",NEW)!=NEW:
        raise RuntimeError("Pinned hotfix commit missing: git fetch the branch")
    changed=set(run("git","-C",str(ROOT),"diff","--name-only",LIVE,NEW).splitlines())
    # LIVE is the immutable release SHA from before the alert deployment
    # scripts were committed. Those script-only additions are expected in the
    # intervening branch history; NEW itself changes only the UI relative to
    # its immediate parent. Pin both sets rather than relaxing this guard.
    expected={UI,"scripts/cutover_owner_alerts_8081.py",
              "scripts/install_owner_alert_timer.py",
              "scripts/prepare_owner_alerts.py",
              "scripts/stage_owner_alerts_8081.py",
              "tests/test_owner_alerts.py"}
    if changed!=expected:
        raise RuntimeError("Unexpected changed files: "+str(sorted(changed.symmetric_difference(expected))))
    previous=run("git","-C",str(ROOT),"rev-parse",NEW+"^")
    if set(run("git","-C",str(ROOT),"diff","--name-only",previous,NEW).splitlines())!={UI}:
        raise RuntimeError("Pinned UI commit contains non-template changes")
    sys.path.insert(0,str(RELEASES/LIVE/"scripts"))
    import deploy_admin_bluegreen as bg
    bg.LOCK.parent.mkdir(parents=True,exist_ok=True)
    with bg.LOCK.open("a+") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if bg.routing(bg.nginx_path().read_text())!=8081:
            raise RuntimeError("Expected public 8081")
        if bg.release_sha_for_port(8081)!=LIVE or bg.release_sha_for_port(8082)!=OLD:
            raise RuntimeError("Admin revisions changed since last verified deployment")
        if not bg.health(8081) or not bg.health(8082):
            raise RuntimeError("Both admin instances must be healthy")
        botpid=run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")
        if int(botpid)<1 or run("systemctl","show","dato-telegram-bot.service","-p","WorkingDirectory","--value")!=str(RELEASES/BOT):
            raise RuntimeError("Unexpected bot process")
        if bg.memory_mib()<180 or shutil.disk_usage(ROOT).free<1024**3:
            raise RuntimeError("Insufficient resources")
        release=bg.snapshot(NEW)
        if not (release/UI).is_file() or MARK not in (release/UI).read_text():
            raise RuntimeError("Required inline feedback is absent")
        python=bg.ensure_venv(release)
        for pattern in ("test_owner_alerts.py","test_miniapp_analytics.py","test_members_miniapp.py","test_support_permissions.py"):
            subprocess.run([str(python),"-m","unittest","discover","-s",str(release/"tests"),"-p",pattern],
                           cwd=release,check=True,timeout=120)
        html=(release/UI).read_text()
        snippets=re.findall(r"<script>(.*?)</script>",html,re.S)
        snippets=[v for v in snippets if v.strip()]
        if len(snippets)!=1:raise RuntimeError("Unexpected JS layout")
        if shutil.which("node"):
            with tempfile.NamedTemporaryFile("w",suffix=".js",delete=False) as f:
                f.write(snippets[0]);filename=f.name
            try:subprocess.run(["node","--check",filename],check=True,timeout=10)
            finally:Path(filename).unlink(missing_ok=True)
        old_unit=Path("/etc/systemd/system")/bg.UNITS[8082]
        if not old_unit.is_file() or run("systemctl","is-active",bg.UNITS[8082])!="active":
            raise RuntimeError("Backup service missing")
        original=old_unit.read_text()
        print("STAGING UI-ONLY HOTFIX ON INACTIVE 8082",flush=True)
        stopped=False
        try:
            bg.command("systemctl","stop",bg.UNITS[8082])
            stopped=True
            if bg.health(8082):raise RuntimeError("Standby port still responding")
            old_unit.write_text(bg.unit_content(release,8082))
            old_unit.chmod(0o644)
            bg.command("systemctl","daemon-reload")
            bg.command("systemctl","start",bg.UNITS[8082])
            for i in range(75):
                if bg.health(8082):break
                if bg.command("systemctl","is-active",bg.UNITS[8082],check=False)!="active":
                    raise RuntimeError("New standby exited")
                time.sleep(1)
            else:raise RuntimeError("New standby startup timeout")
            status,body=get("http://127.0.0.1:8082/tg-support/analytics")
            if status!=200 or MARK not in body:raise RuntimeError("Feedback UI absent on staged release")
            for path in ("/tg-support/api/owner-alerts","/tg-support/api/analytics"):
                if get("http://127.0.0.1:8082"+path)[0]!=401:
                    raise RuntimeError("Signed API guard failed: "+path)
            if bg.routing(bg.nginx_path().read_text())!=8081 or not bg.health(8081):
                raise RuntimeError("Public instance unexpectedly changed")
            if run("systemctl","show","dato-telegram-bot.service","-p","MainPID","--value")!=botpid:
                raise RuntimeError("Bot PID changed")
        except BaseException:
            if stopped:
                print("Restoring previous standby 8082...",flush=True)
                bg.command("systemctl","stop",bg.UNITS[8082],check=False)
                old_unit.write_text(original)
                bg.command("systemctl","daemon-reload",check=False)
                bg.command("systemctl","start",bg.UNITS[8082],check=False)
            raise
        print("INLINE FEEDBACK STAGED: 8082",NEW[:12],flush=True)
        print("PUBLIC UNCHANGED: 8081",LIVE[:12],flush=True)
        print("BOT UNCHANGED PID:",botpid,flush=True)
        print("OWNER ALERT TIMER UNCHANGED; CURRENT NOTIFICATIONS CONTINUE",flush=True)

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("UI HOTFIX STAGE STOPPED:",type(e).__name__,str(e).splitlines()[0][:350],file=sys.stderr)
        sys.exit(1)
